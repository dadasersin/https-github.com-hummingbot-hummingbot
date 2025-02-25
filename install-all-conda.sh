#!/bin/bash

cd $(dirname $0)

source $(dirname "$0")/setup/common_install.sh

echo "*** Installing Conda environment for Hummingbot ***"

CONDA_EXE=$(find_conda_exe) || exit 1
CONDA_BIN=$(dirname ${CONDA_EXE})

_ENV_DIR="setup"
ENV_FILE="environment.yml"

ENV_FILE=$(get_env_file "${_ENV_DIR}/${ENV_FILE}") || exit 1

ENV_NAME=$(get_env_name "${_ENV_DIR}/${ENV_FILE}")

use_mamba=$(get_env_var "USE_MAMBA")

if ${CONDA_EXE} env list | awk '{ print $1 }' | egrep -e "^${ENV_NAME}$" 1>/dev/null; then
  if [ "$use_mamba" == "yes" ]; then
    ${CONDA_EXE} install -n ${ENV_NAME} mamba -y
    ${CONDA_EXE} run -n ${ENV_NAME} mamba env update --file "${_ENV_DIR}/${ENV_FILE}"
  else
    ${CONDA_EXE} env update -f "${_ENV_DIR}/${ENV_FILE}" --prune
  fi
else
  if [[ -n "${USE_MAMBA}" ]]; then
    ${CONDA_EXE} create -n ${ENV_NAME} python=3.10 mamba -y
    ${CONDA_EXE} run -n ${ENV_NAME} mamba env update --file "${_ENV_DIR}/${ENV_FILE}"
  else
    ${CONDA_EXE} env create -f "${_ENV_DIR}/${ENV_FILE}"
  fi
fi

if [ $? -ne 0 ]; then
    echo "Failed to update the conda environment. Please resolve the above errors and try again."
    exit 1
fi

source "${CONDA_BIN}/activate" ${ENV_NAME}

# Installing conflicting pip packages without their dependencies
# They could override the environment plan with conda
if [ "${ENV_NAME}" == "hummingbot-development" ]; then
  echo "Skipping pip packages installation for development environment"
else
  echo "Installing conflicting pip packages without their dependencies"
  read -t 10 -p "Do you want to continue? [Y/n]: " RESPONSE
  if [ "${RESPONSE}_" == "_" ] || [ "${RESPONSE}" != "n" ]; then
    python -m pip install --no-deps -r setup/pip_packages.txt 1> logs/pip_install.log 2>&1
  fi
fi

# Record updated environment file
${CONDA_EXE} env export -n ${ENV_NAME} > "${_ENV_DIR}/x-installed:${ENV_FILE}"
# Directly reformat the exported environment file
update_environment_yml "${_ENV_DIR}/${ENV_FILE}" "${_ENV_DIR}/x-installed:${ENV_FILE}" "${_ENV_DIR}/x-installed:${ENV_FILE}"

pre-commit install
