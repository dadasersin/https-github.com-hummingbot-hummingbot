import asyncio
import re
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from bidict import bidict

from hummingbot.connector.constants import s_decimal_NaN
from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS, kraken_web_utils as web_utils
from hummingbot.connector.exchange.kraken.kraken_api_order_book_data_source import KrakenAPIOrderBookDataSource
from hummingbot.connector.exchange.kraken.kraken_api_user_stream_data_source import KrakenAPIUserStreamDataSource
from hummingbot.connector.exchange.kraken.kraken_auth import KrakenAuth
from hummingbot.connector.exchange.kraken.kraken_constants import KrakenAPITier
from hummingbot.connector.exchange.kraken.kraken_utils import (
    build_rate_limits_by_tier,
    convert_from_exchange_symbol,
    convert_from_exchange_trading_pair,
)
from hummingbot.connector.exchange_py_base import ExchangePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import get_new_numeric_client_order_id
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.utils.tracking_nonce import NonceCreator
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter


class KrakenExchange(ExchangePyBase):
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 10.0
    SHORT_POLL_INTERVAL = 30.0

    web_utils = web_utils
    REQUEST_ATTEMPTS = 5

    def __init__(self,
                 client_config_map: "ClientConfigAdapter",
                 kraken_api_key: str,
                 kraken_secret_key: str,
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 domain: str = CONSTANTS.DEFAULT_DOMAIN,
                 kraken_api_tier: str = "starter"
                 ):
        self.api_key = kraken_api_key
        self.secret_key = kraken_secret_key
        self._domain = domain
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._kraken_api_tier = KrakenAPITier(kraken_api_tier.upper() if kraken_api_tier else "STARTER")
        self._asset_pairs = {}
        self._client_config = client_config_map
        self._client_order_id_nonce_provider = NonceCreator.for_microseconds()
        self._throttler = self._build_async_throttler(api_tier=self._kraken_api_tier)

        self.check_network_timeout = 10.0

        super().__init__(client_config_map)

    def __repr__(self) -> str:
        rep: str = (
            f"KrakenExchange({self._domain})\n"
            f"  - trading_pairs: {self._trading_pairs}\n"
            f"  - trading_required: {self._trading_required}\n"
            f"  - asset_uuid_map: {self._asset_uuid_map}\n"
            f"  - market_assets_initialized: {self._market_assets_initialized}\n"
            f"  - pair_symbol_map_initialized: {self._market_assets}\n"
            f"  - time_synchronizer: {self._time_synchronizer}\n"
            f"  - last_poll_timestamp: {self._last_poll_timestamp}\n"
            f"  - in_flight_orders: {self._order_tracker.active_orders}\n"
            f"  - status_dict: {self.status_dict}\n"
        )
        return rep

    @staticmethod
    def kraken_order_type(order_type: OrderType) -> str:
        return order_type.name.lower()

    @staticmethod
    def to_hb_order_type(kraken_type: str) -> OrderType:
        return OrderType[kraken_type]

    @property
    def authenticator(self):
        return KrakenAuth(
            api_key=self.api_key,
            secret_key=self.secret_key,
            time_provider=self._time_synchronizer)

    @property
    def name(self) -> str:
        return "kraken"

    @property
    def status_dict(self) -> Dict[str, bool]:
        # self.logger().debug(
        #     f"\n   symbols_mapping_initialized: {self.trading_pair_symbol_map_ready()}\n"
        #     f"   order_books_initialized: {self.order_book_tracker.ready}\n"
        #     f"   account_balance: {len(self._account_balances) > 0}\n"
        #     f"   account_balance: {len(self._account_available_balances) > 0}\n"
        #     f"   trading_required: {self.is_trading_required}\n"
        #     f"   trading_rule_initialized: {len(self._trading_rules) > 0 if self.is_trading_required else True}\n"
        #     f"   user_stream_initialized: {self._is_user_stream_initialized()}\n"
        # )
        return {
            "symbols_mapping_initialized": self.trading_pair_symbol_map_ready(),
            "order_books_initialized": self.order_book_tracker.ready,
            "account_balance": not self.is_trading_required or len(self._account_balances) > 0,
            "trading_rule_initialized": len(self._trading_rules) > 0 if self.is_trading_required else True,
            "user_stream_initialized": self._is_user_stream_initialized(),
        }

    # not used
    @property
    def rate_limits_rules(self):
        return build_rate_limits_by_tier(self._kraken_api_tier)

    @property
    def domain(self):
        return self._domain

    @property
    def client_order_id_max_length(self):
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self):
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self):
        return CONSTANTS.ASSET_PAIRS_PATH_URL

    @property
    def trading_pairs_request_path(self):
        return CONSTANTS.ASSET_PAIRS_PATH_URL

    @property
    def check_network_request_path(self):
        return CONSTANTS.STATUS_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return True

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    def supported_order_types(self):
        return [
            OrderType.LIMIT,
            OrderType.LIMIT_MAKER,
            OrderType.MARKET,
            OrderType.STOP_LOSS,
            OrderType.TAKE_PROFIT,
            OrderType.TRAILING_STOP,
            OrderType.STOP_LOSS_LIMIT,
            OrderType.TAKE_PROFIT_LIMIT,
            OrderType.TRAILING_STOP_LIMIT,
        ]

    def _build_async_throttler(self, api_tier: KrakenAPITier) -> AsyncThrottler:
        limits_pct = self._client_config.rate_limits_share_pct
        if limits_pct < Decimal("100"):
            self.logger().warning(
                f"The Kraken API does not allow enough bandwidth for a reduced rate-limit share percentage."
                f" Current percentage: {limits_pct}."
            )
        throttler = AsyncThrottler(build_rate_limits_by_tier(api_tier))
        return throttler

    async def _update_time_synchronizer(self, pass_on_non_cancelled_error: bool = False):
        # Overriding ExchangePyBase: Synchronizer expects time in ms
        try:
            await self._time_synchronizer.update_server_time_offset_with_time_provider(
                time_provider=self.web_utils.get_current_server_time_ms(
                    throttler=self._throttler,
                    domain=self.domain,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if not pass_on_non_cancelled_error:
                self.logger().exception(f"Error requesting time from {self.name_cap} server")
                raise

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        return False

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        return False

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        return CONSTANTS.UNKNOWN_ORDER_MESSAGE in str(cancelation_exception)

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        return web_utils.build_api_factory(
            throttler=self._throttler,
            auth=self._auth)

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        return KrakenAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory)

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        return KrakenAPIUserStreamDataSource(
            connector=self,
            api_factory=self._web_assistants_factory,
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        is_maker = order_type is OrderType.LIMIT_MAKER
        trade_base_fee = build_trade_fee(
            exchange=self.name,
            is_maker=is_maker,
            order_side=order_side,
            order_type=order_type,
            amount=amount,
            price=price,
            base_currency=base_currency,
            quote_currency=quote_currency
        )
        return trade_base_fee

    async def _api_get(self, *args, **kwargs):
        kwargs["method"] = RESTMethod.GET
        return await self._api_request_with_retry(*args, **kwargs)

    async def _api_post(self, *args, **kwargs):
        kwargs["method"] = RESTMethod.POST
        return await self._api_request_with_retry(*args, **kwargs)

    async def _api_put(self, *args, **kwargs):
        kwargs["method"] = RESTMethod.PUT
        return await self._api_request_with_retry(*args, **kwargs)

    async def _api_delete(self, *args, **kwargs):
        kwargs["method"] = RESTMethod.DELETE
        return await self._api_request_with_retry(*args, **kwargs)

    @staticmethod
    def is_cloudflare_exception(exception: Exception):
        """
        Error status 5xx or 10xx are related to Cloudflare.
        https://support.kraken.com/hc/en-us/articles/360001491786-API-error-messages#6
        """
        return bool(re.search(r"HTTP status is (5|10)\d\d\.", str(exception)))

    @staticmethod
    def is_market_service_exception(exception: Exception):
        """
        Error status Market in cancel-only mode
        """
        return "EService:Market in cancel_only mode" in str(exception)

    async def get_open_orders_with_userref(self, userref: int):
        data = {'userref': userref}
        return await self._api_request_with_retry(
            RESTMethod.POST,
            CONSTANTS.OPEN_ORDERS_PATH_URL,
            is_auth_required=True,
            data=data
        )

    # === Orders placing ===

    def buy(self,
            trading_pair: str,
            amount: Decimal,
            order_type=OrderType.LIMIT,
            price: Decimal = s_decimal_NaN,
            **kwargs) -> str:
        """
        Creates a promise to create a buy order using the parameters

        :param trading_pair: the token pair to operate with
        :param amount: the order amount
        :param order_type: the type of order to create (MARKET, LIMIT, LIMIT_MAKER, STOP_LOSS, TAKE_PROFIT, TRAILING_STOP)
        :param price: the order price

        :return: the id assigned by the connector to the order (the client id)
        """
        order_id = str(get_new_numeric_client_order_id(
            nonce_creator=self._client_order_id_nonce_provider,
            max_id_bit_count=CONSTANTS.MAX_ID_BIT_COUNT,
        ))
        safe_ensure_future(
            self._create_order(
                trade_type=TradeType.BUY,
                order_id=order_id,
                trading_pair=trading_pair,
                amount=amount,
                order_type=order_type,
                price=price,
                **kwargs,
            )
        )
        return order_id

    def sell(self,
             trading_pair: str,
             amount: Decimal,
             order_type: OrderType = OrderType.LIMIT,
             price: Decimal = s_decimal_NaN,
             **kwargs) -> str:
        """
        Creates a promise to create a sell order using the parameters.
        :param trading_pair: the token pair to operate with
        :param amount: the order amount
        :param order_type: the type of order to create (MARKET, LIMIT, LIMIT_MAKER, STOP_LOSS, TAKE_PROFIT, TRAILING_STOP)
        :param price: the order price
        :return: the id assigned by the connector to the order (the client id)
        """
        order_id = str(get_new_numeric_client_order_id(
            nonce_creator=self._client_order_id_nonce_provider,
            max_id_bit_count=CONSTANTS.MAX_ID_BIT_COUNT,
        ))
        safe_ensure_future(
            self._create_order(
                trade_type=TradeType.SELL,
                order_id=order_id,
                trading_pair=trading_pair,
                amount=amount,
                order_type=order_type,
                price=price,
                **kwargs,
            )
        )
        return order_id

    async def get_asset_pairs(self) -> Dict[str, Any]:
        if not self._asset_pairs:
            asset_pairs = await self._api_request_with_retry(method=RESTMethod.GET,
                                                             path_url=CONSTANTS.ASSET_PAIRS_PATH_URL)
            self._asset_pairs = {f"{details['base']}-{details['quote']}": details
                                 for _, details in asset_pairs.items() if
                                 web_utils.is_exchange_information_valid(details)}
        return self._asset_pairs

    async def _place_order(self,
                           order_id: str,
                           trading_pair: str,
                           amount: Decimal,
                           trade_type: TradeType,
                           order_type: OrderType,
                           price: Decimal,
                           **kwargs) -> Tuple[str, float]:
        trading_pair = await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        data = {
            "pair": trading_pair,
            "type": "buy" if trade_type is TradeType.BUY else "sell",
            "volume": str(amount),
            "userref": order_id,  # This is a non-unique field, useful to group batches of orders
            # "cl_order_id": order_id, # Kraken supports unique client order id
            "price": str(price),
            # "timeinforce": "GTC",
            # "starttm": "0",
            # "expiretm": "0",
        }

        if kwargs.get("price_in_percent", False):
            data["price"] = f"#{price}%"

        if (
            order_type
            in {
                OrderType.STOP_LOSS,
                OrderType.STOP_LOSS_LIMIT,
                OrderType.TAKE_PROFIT,
                OrderType.TAKE_PROFIT_LIMIT,
                OrderType.TRAILING_STOP,
                OrderType.TRAILING_STOP_LIMIT,
            }
            and "price_in_percent" not in kwargs
        ):
            self.logger().debug(f"kwargs: {kwargs}")
            raise ValueError(f"{order_type} order requires to clarify if price is in percent with 'price_in_percent=True/False'")

        if (
            order_type
            in {
                OrderType.STOP_LOSS_LIMIT,
                OrderType.TAKE_PROFIT_LIMIT,
                OrderType.TRAILING_STOP_LIMIT,
            }
        ):
            if "price2" not in kwargs and "limit_price" not in kwargs:
                self.logger().debug(f"kwargs: {kwargs}")
                raise ValueError(f"{order_type} order requires a limit price: 'price2=str or limit_price=str'")
            if "price2" in kwargs and "limit_price" in kwargs:
                self.logger().debug(f"kwargs: {kwargs}")
                raise ValueError(f"{order_type} order cannot specify both: 'price2=str and limit_price=str'")
            price2: Decimal = kwargs.get("price2", kwargs.get("limit_price"))
            if not isinstance(price2, Decimal):
                self.logger().debug(f"kwargs: {kwargs}")
                raise ValueError(f"{order_type} order limit price must be Decimal")
            data["price2"] = f"{price2:+}%"

        if order_type is OrderType.MARKET:
            data["ordertype"] = "market"
            del data["price"]

        elif order_type is OrderType.LIMIT:
            data["ordertype"] = "limit"

        elif order_type is OrderType.LIMIT_MAKER:
            data["ordertype"] = "limit"
            data["oflags"] = "post"

        elif order_type is OrderType.STOP_LOSS:
            data["ordertype"] = "stop-loss"

        elif order_type is OrderType.STOP_LOSS_LIMIT:
            data["ordertype"] = "stop-loss-limit"

        elif order_type is OrderType.TAKE_PROFIT:
            data["ordertype"] = "take-profit"

        elif order_type is OrderType.TAKE_PROFIT_LIMIT:
            data["ordertype"] = "take-profit-limit"

        elif order_type is OrderType.TRAILING_STOP:
            data["ordertype"] = "trailing-stop"
            data["price"] = data["price"].replace("#", "+")

        elif order_type is OrderType.TRAILING_STOP_LIMIT:
            data["ordertype"] = "trailing-stop-limit"
            data["price"] = data["price"].replace("#", "+")

        elif hasattr(order_type, "name"):
            raise ValueError(f"Order type {order_type.name} not supported")
        else:
            raise ValueError(f"Order type {order_type} is invalid")

        self.logger().debug(f"  '-> Placing order {order_id} for {amount} {trading_pair} at {price} {trade_type.name} {order_type} with {kwargs}")
        self.logger().debug(f"  '-> request data {data}")
        order_result = await self._api_request_with_retry(RESTMethod.POST,
                                                          CONSTANTS.ADD_ORDER_PATH_URL,
                                                          data=data,
                                                          is_auth_required=True)

        o_id = order_result["txid"][0]
        self.logger().debug(f"  '-> Placed {order_type}")
        return o_id, self.current_timestamp

    async def _api_request_with_retry(self,
                                      method: RESTMethod,
                                      path_url: str,
                                      params: Optional[Dict[str, Any]] = None,
                                      data: Optional[Dict[str, Any]] = None,
                                      is_auth_required: bool = False,
                                      retry_interval=2.0) -> Dict[str, Any]:
        response_json = None
        result = None
        for retry_attempt in range(self.REQUEST_ATTEMPTS):
            self.logger().debug(f"Sending {method} request to {path_url} with params {params} and data {data}")
            try:
                response_json = await self._api_request(path_url=path_url, method=method, params=params, data=data,
                                                        is_auth_required=is_auth_required)

                if response_json.get("error") or not response_json.get("result"):
                    raise IOError({"error": response_json})

                result = response_json.get("result")
                break
            except IOError as e:
                if self.is_cloudflare_exception(e):
                    if path_url == CONSTANTS.ADD_ORDER_PATH_URL:
                        self.logger().info(f"Retrying {path_url}")
                        # Order placement could have been successful despite the IOError, so check for the open order.
                        response = await self.get_open_orders_with_userref(data.get('userref'))
                        if any(response.get("open").values()):
                            return response

                    self.logger().warning(
                        f"Cloudflare error. Attempt {retry_attempt + 1}/{self.REQUEST_ATTEMPTS}"
                        f" API command {method}: {path_url}"
                    )
                    await asyncio.sleep(retry_interval ** retry_attempt)
                    continue

                elif self.is_market_service_exception(e):
                    self.logger().error(f"Market in cancel-only mode error from {path_url}.")
                    await asyncio.sleep((10 * retry_interval) ** retry_attempt)
                    continue

                elif isinstance(e, dict) and "EAPI:Invalid nonce" in e.get("error", ""):
                    self.logger().error(f"Invalid nonce error from {path_url}. " +
                                        "Please ensure your Kraken API key nonce window is at least 10, " +
                                        "and if needed reset your API key.")
                    raise ValueError("Invalid nonce error from Kraken API")

                else:
                    self.logger().error(f"Error fetching data from {path_url}, msg is {response_json}")
                    raise e
        if not result:
            raise IOError(f"Error fetching data from {path_url}, msg is {response_json}.")

        return result

    async def _get_exchange_order_id(self, tracked_order: InFlightOrder) -> str:
        if (exchange_order_id := tracked_order.exchange_order_id) is None:
            response = await self.get_open_orders_with_userref(int(tracked_order.client_order_id))
            if any(response.get("open").values()):
                exchange_order_id = list(response.get("open").keys())[0]
            else:
                exchange_order_id = None
        return exchange_order_id

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        if not (exchange_order_id := await self._get_exchange_order_id(tracked_order)):
            return False

        self.logger().info(f"  '-> Cancelling order {order_id} with exchange id {exchange_order_id}")
        api_params = {
            "txid": exchange_order_id,
        }
        cancel_result = await self._api_request_with_retry(
            method=RESTMethod.POST,
            path_url=CONSTANTS.CANCEL_ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True)
        self.logger().info("  '-> Placed")
        return isinstance(cancel_result, dict) and (
            cancel_result.get("count") == 1
            or cancel_result.get("error") is not None
        )

    # --- Re-implement cancel_all to benefit from batch cancel exchange API ---

#    async def cancel_all(self, timeout_seconds: float) -> List[CancellationResult]:
#        """
#        Cancels all currently active orders. The cancellations are performed in parallel tasks.
#
#        :param timeout_seconds: the maximum time (in seconds) the cancel logic should run
#
#        :return: a list of CancellationResult instances, one for each of the orders to be cancelled
#        """
#
#        async def execute_cancels(order_ids: List[str]) -> bool:
#            """
#            Requests the exchange to cancel an active order
#
#            :param order_ids: the client id of the orders to cancel
#            """
#            tracked_orders = [(o, t) for o in order_ids if (t := self._order_tracker.fetch_tracked_order(o))]
#            valid_orders = [t[1] for t in tracked_orders if t[1] is not None]
#            result = await execute_orders_cancel_and_process_update(orders=valid_orders)
#            return result
#
#        async def execute_orders_cancel_and_process_update(orders: List[InFlightOrder]) -> bool:
#            cancelled = await self._place_cancels(order_ids=[o.exchange_order_id for o in orders])
#            if cancelled:
#                for o in orders:
#                    update_timestamp = self.current_timestamp
#                    if update_timestamp is None or math.isnan(update_timestamp):
#                        update_timestamp = self.time_synchronizer.time()
#
#                    order_update: OrderUpdate = OrderUpdate(
#                        client_order_id=o.client_order_id,
#                        trading_pair=o.trading_pair,
#                        update_timestamp=update_timestamp,
#                        new_state=(OrderState.CANCELED
#                                   if self.is_cancel_request_in_exchange_synchronous
#                                   else OrderState.PENDING_CANCEL),
#                    )
#                    self._order_tracker.process_order_update(order_update)
#
#            return cancelled
#
#        incomplete_orders = [o for o in self.in_flight_orders.values() if not o.is_done]
#        order_id_set = {o.client_order_id for o in incomplete_orders}
#        successful_cancellations = []
#
#        try:
#            async with timeout(timeout_seconds):
#                cancellation_result = await execute_cancels(order_ids=[o.client_order_id for o in incomplete_orders])
#                for cr in cancellation_results:
#                    if isinstance(cr, Exception):
#                        continue
#                    client_order_id = cr
#                    if client_order_id is not None:
#                        order_id_set.remove(client_order_id)
#                        successful_cancellations.append(CancellationResult(client_order_id, True))
#        except Exception:
#            self.logger().network(
#                "Unexpected error cancelling orders.",
#                exc_info=True,
#                app_warning_msg="Failed to cancel order. Check API key and network connection."
#            )
#        failed_cancellations = [CancellationResult(oid, False) for oid in order_id_set]
#        return successful_cancellations + failed_cancellations
#
#    async def _place_cancel_batch(self, tracked_orders: List[InFlightOrder]):
#        exchange_order_ids = [await self._.get_exchange_order_id(tracked_order) for tracked_order in tracked_orders]
#        exchange_order_ids = [e for e in exchange_order_ids if e is not None]
#        tasks = []
#        for batch in [exchange_order_ids[i:i + CONSTANTS.MAX_CANCEL_BATCH_SIZE] for i in range(0, len(exchange_order_ids), 50)]:
#            api_params = {
#                "orders": batch,
#            }
#            tasks.append(self._api_request(
#                method=RESTMethod.POST,
#                path_url=CONSTANTS.CANCEL_ORDER_BATCH_PATH_URL,
#                data=api_params,
#                is_auth_required=True))
#        cancel_result = await safe_gather(*tasks, return_exceptions=True)
#        return all(
#            isinstance(cr, dict)
#            and (
#                cr.get("count") == len(exchange_order_ids)
#                or cr.get("error") is not None
#            )
#            for cr in cancel_result
#        )

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Example:
        {
            "XBTUSDT": {
              "altname": "XBTUSDT",
              "wsname": "XBT/USDT",
              "aclass_base": "currency",
              "base": "XXBT",
              "aclass_quote": "currency",
              "quote": "USDT",
              "lot": "unit",
              "pair_decimals": 1,
              "lot_decimals": 8,
              "lot_multiplier": 1,
              "leverage_buy": [2, 3],
              "leverage_sell": [2, 3],
              "fees": [
                [0, 0.26],
                [50000, 0.24],
                [100000, 0.22],
                [250000, 0.2],
                [500000, 0.18],
                [1000000, 0.16],
                [2500000, 0.14],
                [5000000, 0.12],
                [10000000, 0.1]
              ],
              "fees_maker": [
                [0, 0.16],
                [50000, 0.14],
                [100000, 0.12],
                [250000, 0.1],
                [500000, 0.08],
                [1000000, 0.06],
                [2500000, 0.04],
                [5000000, 0.02],
                [10000000, 0]
              ],
              "fee_volume_currency": "ZUSD",
              "margin_call": 80,
              "margin_stop": 40,
              "ordermin": "0.0002"
            }
        }
        """
        retval: list = []
        trading_pair_rules = exchange_info_dict.values()
        for rule in filter(web_utils.is_exchange_information_valid, trading_pair_rules):
            try:
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol=rule.get("altname"))
                min_order_size = Decimal(rule.get('ordermin', 0))
                min_price_increment = Decimal(f"1e-{rule.get('pair_decimals')}")
                min_base_amount_increment = Decimal(f"1e-{rule.get('lot_decimals')}")
                retval.append(
                    TradingRule(
                        trading_pair,
                        min_order_size=min_order_size,
                        min_price_increment=min_price_increment,
                        min_base_amount_increment=min_base_amount_increment,
                    )
                )
            except Exception:
                self.logger().error(f"Error parsing the trading pair rule {rule}. Skipping.", exc_info=True)
        return retval

    async def _update_trading_fees(self):
        """
        Update fees information from the exchange
        """
        pass

    async def _user_stream_event_listener(self):
        """
        Listens to messages from _user_stream_tracker.user_stream queue.
        Traders, Orders, and Balance updates from the WS.
        """
        async for event_message in self._iter_user_event_queue():
            try:
                if isinstance(event_message, list):
                    channel: str = event_message[-2]
                    results: List[Any] = event_message[0]
                    if channel == CONSTANTS.USER_TRADES_ENDPOINT_NAME:
                        self._process_trade_message(results)
                    elif channel == CONSTANTS.USER_ORDERS_ENDPOINT_NAME:
                        self._process_order_message(event_message)
                elif event_message is asyncio.CancelledError:
                    raise asyncio.CancelledError
                else:
                    raise Exception(event_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(
                    "Unexpected error in user stream listener loop.", exc_info=True)
                await self._sleep(5.0)

    def _create_trade_update_with_order_fill_data(
            self,
            order_fill: Dict[str, Any],
            order: InFlightOrder):
        fee_asset = order.quote_asset

        fee = TradeFeeBase.new_spot_fee(
            fee_schema=self.trade_fee_schema(),
            trade_type=order.trade_type,
            percent_token=fee_asset,
            flat_fees=[TokenAmount(
                amount=Decimal(order_fill["fee"]),
                token=fee_asset
            )]
        )
        trade_update = TradeUpdate(
            trade_id=str(order_fill["trade_id"]),
            client_order_id=order.client_order_id,
            exchange_order_id=order_fill.get("ordertxid"),
            trading_pair=order.trading_pair,
            fee=fee,
            fill_base_amount=Decimal(order_fill["vol"]),
            fill_quote_amount=Decimal(order_fill["vol"]) * Decimal(order_fill["price"]),
            fill_price=Decimal(order_fill["price"]),
            fill_timestamp=order_fill["time"],
        )
        return trade_update

    def _process_trade_message(self, trades: List):
        for update in trades:
            trade_id: str = next(iter(update))
            trade: Dict[str, str] = update[trade_id]
            trade["trade_id"] = trade_id
            exchange_order_id = trade.get("ordertxid")
            client_order_id = str(trade.get("userref", ""))
            tracked_order = self._order_tracker.all_fillable_orders.get(client_order_id)

            if not tracked_order:
                self.logger().debug(f"Ignoring trade message with id {exchange_order_id}: not in in_flight_orders.")
            else:
                trade_update = self._create_trade_update_with_order_fill_data(
                    order_fill=trade,
                    order=tracked_order)
                self._order_tracker.process_trade_update(trade_update)

    def _create_order_update_with_order_status_data(self, order_status: Dict[str, Any], order: InFlightOrder):
        order_update = OrderUpdate(
            trading_pair=order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=CONSTANTS.ORDER_STATE[order_status["status"]],
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id,
        )
        return order_update

    def _process_order_message(self, orders: List):
        update = orders[0]
        for message in update:
            for exchange_order_id, order_msg in message.items():
                client_order_id = str(order_msg.get("userref", ""))
                tracked_order = self._order_tracker.all_updatable_orders.get(client_order_id)
                if not tracked_order:
                    self.logger().debug(
                        f"Ignoring order message with id {order_msg}: not in in_flight_orders.")
                    return
                if "status" in order_msg:
                    order_update = self._create_order_update_with_order_status_data(order_status=order_msg,
                                                                                    order=tracked_order)
                    self._order_tracker.process_order_update(order_update=order_update)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        trade_updates = []

        if not (exchange_order_id := await self._get_exchange_order_id(order)):
            self.logger().warning(f"Skipped order update with order fills for {order.client_order_id} - no exchange order id or registered client order id.")
            return trade_updates

        try:
            all_fills_response = await self._api_request_with_retry(
                method=RESTMethod.POST,
                path_url=CONSTANTS.QUERY_TRADES_PATH_URL,
                data={"txid": exchange_order_id},
                is_auth_required=True)
        except Exception as e:
            if "EOrder:Unknown order" in str(e) or "EOrder:Invalid order" in str(e):
                return trade_updates
            else:
                raise

        for trade_id, trade_fill in all_fills_response.items():
            trade: Dict[str, str] = all_fills_response[trade_id]
            trade["trade_id"] = trade_id
            trade_update = self._create_trade_update_with_order_fill_data(
                order_fill=trade,
                order=order)
            trade_updates.append(trade_update)

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        if not (exchange_order_id := await self._get_exchange_order_id(tracked_order)):
            raise ValueError(f"Skipped order status update for {tracked_order.client_order_id} - no exchange order id or registeres client order id.")

        updated_order_data = await self._api_request_with_retry(
            method=RESTMethod.POST,
            path_url=CONSTANTS.QUERY_ORDERS_PATH_URL,
            data={"txid": exchange_order_id},
            is_auth_required=True)

        update = updated_order_data.get(exchange_order_id)
        new_state = CONSTANTS.ORDER_STATE[update["status"]]

        order_update = OrderUpdate(
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=new_state,
        )
        self.logger().debug(f"Order status update: {order_update}")
        return order_update

    async def _update_balances(self):
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()
        balances = await self._api_request_with_retry(RESTMethod.POST, CONSTANTS.BALANCE_PATH_URL,
                                                      is_auth_required=True)
        open_orders = await self._api_request_with_retry(RESTMethod.POST, CONSTANTS.OPEN_ORDERS_PATH_URL,
                                                         is_auth_required=True)

        locked = defaultdict(Decimal)

        for order in open_orders.get("open").values():
            if order.get("status") == "open":
                details = order.get("descr")
                if details.get("ordertype") == "limit":
                    pair = convert_from_exchange_trading_pair(
                        details.get("pair"), tuple((await self.get_asset_pairs()).keys())
                    )
                    (base, quote) = self.split_trading_pair(pair)
                    vol_locked = Decimal(order.get("vol", 0)) - Decimal(order.get("vol_exec", 0))
                    if details.get("type") == "sell":
                        locked[convert_from_exchange_symbol(base)] += vol_locked
                    elif details.get("type") == "buy":
                        locked[convert_from_exchange_symbol(quote)] += vol_locked * Decimal(details.get("price"))

        for asset_name, balance in balances.items():
            cleaned_name = convert_from_exchange_symbol(asset_name).upper()
            total_balance = Decimal(balance)
            free_balance = total_balance - Decimal(locked[cleaned_name])
            self._account_available_balances[cleaned_name] = free_balance
            self._account_balances[cleaned_name] = total_balance
            remote_asset_names.add(cleaned_name)

        for cleaned_name, ava_balance in self._account_available_balances.items():
            if cleaned_name.endswith(".F"):
                asset_normal_name = cleaned_name.split(".")[0]
                cleaned_normal_name = convert_from_exchange_symbol(asset_normal_name).upper()
                new_total_amount = self._account_available_balances.get(cleaned_normal_name, 0) + ava_balance
                self._account_available_balances.update({cleaned_normal_name: new_total_amount})
                self._account_available_balances.update({cleaned_name: 0})

        for cleaned_name, total_balance in self._account_balances.items():
            if cleaned_name.endswith(".F"):
                asset_normal_name = cleaned_name.split(".")[0]
                cleaned_normal_name = convert_from_exchange_symbol(asset_normal_name).upper()
                new_total_amount = self._account_balances.get(cleaned_normal_name, 0) + total_balance
                self._account_balances.update({cleaned_normal_name: new_total_amount})
                self._account_balances.update({cleaned_name: 0})

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    def _initialize_trading_pair_symbols_from_exchange_info(self, exchange_info: Dict[str, Any]):
        mapping = bidict()
        for symbol_data in filter(web_utils.is_exchange_information_valid, exchange_info.values()):
            mapping[symbol_data["altname"]] = convert_from_exchange_trading_pair(symbol_data["wsname"])
        self._set_trading_pair_symbol_map(mapping)

    async def _get_last_traded_price(self, trading_pair: str) -> float:
        params = {
            "pair": await self.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
        }
        resp_json = await self._api_request_with_retry(
            method=RESTMethod.GET,
            path_url=CONSTANTS.TICKER_PATH_URL,
            params=params
        )
        record = list(resp_json.values())[0]
        return float(record["c"][0])
