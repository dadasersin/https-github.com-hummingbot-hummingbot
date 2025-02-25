#!/bin/bash

cd $(dirname $0)

source $(dirname "$0")/setup/common_install.sh

echo "*** Un-Installing a Conda environment for Hummingbot ***"

CONDA_EXE=$(find_conda_exe) || exit 1

_ENV_DIR="setup"
ENV_FILE="environment.yml"

ENV_FILE=$(get_env_file "${_ENV_DIR}/${ENV_FILE}") || exit 1

ENV_NAME=$(get_env_name "${_ENV_DIR}/${ENV_FILE}")

echo    "  '-> Attempting to remove: ${ENV_NAME}"

if ${CONDA_EXE} env list | awk '{ print $1 }' | egrep -e "^${ENV_NAME}$" 1>/dev/null; then
  echo "  '-> Environment found."
  echo "       Environment .yml: ${ENV_FILE}"
  VALID_ENV_NAME=$(grep  'name:' "${_ENV_DIR}/${ENV_FILE}" | tail -n1 | awk '{ print $2}')
  echo "          .yml env name: ${VALID_ENV_NAME}"

  if [ ${ENV_NAME} != ${VALID_ENV_NAME} ]; then
    echo "*** You are attempting to remove the environment:"
    echo "       ${ENV_NAME}"
    echo "    but the environment name in ${ENV_FILE} is:"
    echo "       ${VALID_ENV_NAME}"
  fi

  read -r -t 10 -p "Do you want to continue? [y/N]: " RESPONSE
  if [ "${RESPONSE}_" == "_" ] || [ "${RESPONSE}" != "y" ]; then
    echo ""
    echo "Aborting."
    exit 1
  fi

  ${CONDA_EXE} env remove -n ${ENV_NAME}
  rm -f "${_ENV_DIR}/x-installed:${ENV_FILE}"
else
  echo "Environment already removed."
fi
