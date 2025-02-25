#!/bin/bash

# Function to find conda in the hard-coded Github miniconda path
_find_conda_in_dir() {
    local root_path="$1"
    local maxdepth="$2"

    if [[ ! -d "${root_path}" ]]; then
      return
    fi

    if [[ -x "${root_path}/conda" ]] && [[ -e "${root_path}/activate" ]]; then
      echo "${root_path}/conda"
      return
    fi

    if [[ -z "${maxdepth}" ]]; then
      maxdepth=5
    fi

    local conda_runner_path=""
    while IFS= read -r -d $'\0'; do
      conda_runner_path="$REPLY"
    done < <(find "${root_path}" \
               -maxdepth "${maxdepth}" \
               -type d \
               -exec sh -c '[ -x "$0"/conda ] && [ -e "$0"/activate ]' {} \; \
               -print0 \
            2> /dev/null)

    if [[ -n "$conda_runner_path" ]]; then
      echo "${conda_runner_path}/conda"
      return
    else
      return
    fi
}

# Function to find conda in the given paths
_find_conda_in_paths() {
    local -a paths=("$@")
    local -a conda_dirs

    echo -n "   " >&2
    for path in "${paths[@]}"; do
      echo -n "." >&2
      local conda_exe=$(_find_conda_in_dir "${path}")
      if [[ -n "${conda_exe}" ]]; then
          conda_dirs+=("${conda_exe}")
      fi
    done
    echo "." >&2

    echo "${conda_dirs[@]}"
}

# Function to find the latest version of conda
_find_latest_conda_version() {
    local -a conda_dirs=("$@")
    local selected_version="0.0.0"
    local selected_conda_exe=""

    echo -n "   " >&2
    for c in "${conda_dirs[@]}"; do
      if [[ ! -x "${c}" ]]; then
        continue
      fi
      echo -n "." >&2
      current_version=$("${c}" info --json 2>/dev/null | jq -r --arg version "$selected_version" '
        .conda_version | split(".") | map(tonumber) as $current_version
        | ($version | split(".") | map(tonumber)) as $version
        | (if $current_version > $version then
            $current_version
          else
            empty
          end) as $selected_version
        | $selected_version | join(".")
      ')

      if [ "${current_version}_" != "_" ]; then
        selected_conda_exe="${c}"
        selected_version=${current_version}
      fi
    done
    echo "." >&2

    echo "${selected_conda_exe}"
}

find_conda_exe() {
  # Github miniconda path
  local conda_exe=$(_find_conda_in_dir "/usr/share/miniconda/bin" 2)

  if [ -z "${conda_exe}" ]; then
    local -a paths=( \
      "/usr/share/*conda*" \
      /usr/local \
      /opt \
      "/root/*conda*" \
    )
    if [[ -n "${CONDA_PATH}" ]]; then
      paths+=("${CONDA_PATH}")
    fi
    if [[ -n "${CONDA_EXE}" ]]; then
      paths+=($(dirname "${CONDA_EXE}"))
    fi
    local conda_dirs=($(_find_conda_in_paths "${paths[@]}"))
    conda_exe=$(_find_latest_conda_version "${conda_dirs[@]}")
  fi

  if [ -z "${conda_exe}" ]; then
    echo "Please install Anaconda w/ Python 3.10+ first" >&2
    echo "See: https://www.anaconda.com/distribution/" >&2
    exit 1
  fi

  echo "Selected: ${conda_exe}" >&2
  echo "${conda_exe}"
}

_verify_path() {
  local path="$1"
  if [[ "${path}" = /* ]]; then
      echo "${path}"
  else
      echo "$(pwd)/${path}"
  fi
}

_list_files_on_pattern(){
  local dir="$1"
  local pattern="$2"

  echo "Searching for files matching '${pattern}' in '${dir}'" >&2
  if [ -z "${pattern}" ]; then
    echo "Please provide a pattern to search for" >&2
    exit 1
  fi

  local -a files
  while IFS= read -r -d $'\0'; do
    files+=("${REPLY}")
  done < <(find "${dir}" -maxdepth 1 -type f -name "${pattern}" -printf '%P\0')

  echo "${files[@]}"
}

_select_index_from_list() {
  local -n arr=$1
  local prompt_msg="$2"
  local time_out="$3"

  if [ -z "${prompt_msg}" ]; then
    prompt_msg="Selection"
  fi

  if [ -z "${time_out}" ]; then
    time_out=10
  fi

  local user_input

  local i=1
  for item in "${arr[@]}"; do
    echo "   ${i}: ${item}" >&2
    i=$((i+1))
  done

  while true; do
    read -r -t "${time_out}" -p "${prompt_msg} [1-${#arr[@]}] (1): " user_input
    if [ -z "${user_input}" ]; then
        echo "0"
        return
    fi
    if [[ ${user_input} -ge 1 && ${user_input} -le ${#arr[@]} ]]; then
        echo "$((user_input-1))"
        return
    else
        echo "Invalid selection. Please enter a number between 1 and ${#arr[@]}." >&2
    fi
  done
}

get_env_file() {
  local env_file=$1
  local prompt=$2 || "Enter your selection"
  local time_out=$3 || 10
  local initial_dir=$(pwd)

  cd "$(dirname "$(_verify_path "${env_file}")")" || exit

  local env_ext="${env_file##*.}"
  local -a files=($(_list_files_on_pattern "." "*.${env_ext}")) || exit
  IFS=$'\n' files=($(sort <<<"${files[*]}"))
  unset IFS

  echo "Available environments:" >&2
  local selection
  selection=$(_select_index_from_list files "${prompt}" "${time_out}")

  cd "${initial_dir}" || exit
  echo "${files[$((selection))]}"
}

get_env_name() {
  local env_file=$1
  local initial_dir=$(pwd)

  valid_env_name=$(grep  'name:' "${env_file}" | tail -n1 | awk '{ print $2}')

  cd "${initial_dir}" || exit
    # Return valid_env_name by default
  echo "${valid_env_name}"
}

get_env_var() {
  local var_name="$1"
  local var_value="${!var_name}"

  if [ -n "${var_value}" ]; then
    case "${var_value}" in
      1|y|Y|yes|YES|true|TRUE)
        echo "yes"
        return 0
        ;;
      *)
        echo "no"
        return 1
        ;;
    esac
  else
    echo "no"
    return 1
  fi
}

_decipher_pip_package_entry(){
  local package=$1

  if [[ $package == *"=="* ]]; then
    package_name=$(echo "${package}" | cut -d'=' -f1)
    package_version=$(echo "${package}" | cut -d'=' -f3)
    conda_operator="="
  elif [[ $package == *"!="* ]]; then
    package_name=$(echo "${package}" | cut -d'!' -f1)
    package_version=$(echo "${package}" | cut -d'=' -f2)
  elif [[ $package == *"<="* ]]; then
    package_name=$(echo "${package}" | cut -d'<' -f1)
    package_version=$(echo "${package}" | cut -d'=' -f2)
  elif [[ $package == *">="* ]]; then
    package_name=$(echo "${package}" | cut -d'>' -f1)
    package_version=$(echo "${package}" | cut -d'=' -f2)
  elif [[ $package == *"<*"* ]]; then
    package_name=$(echo "${package}" | cut -d'<' -f1)
    package_version=$(echo "${package}" | cut -d'<' -f2)
  elif [[ $package == *">*"* ]]; then
    package_name=$(echo "${package}" | cut -d'>' -f1)
    package_version=$(echo "${package}" | cut -d'>' -f2)
  elif [[ $package == *"~="* ]]; then
    package_name=$(echo "${package}" | cut -d'~' -f1)
    package_version=$(echo "${package}" | cut -d'=' -f2)
  elif [[ $package == *"==="* ]]; then
    package_name=$(echo "${package}" | cut -d'=' -f1)
    package_version=$(echo "${package}" | cut -d'=' -f4)
  else
    # If the package string doesn't contain a version constraint,
    # use the whole string as the package name.
    package_name=$package
  fi

  echo "${package_name} ${operator} ${package_version}"
}

verify_pip_packages() {
  echo "  '-> Verifying conda alternative to pip packages" >&2
  local install_dir="/tmp/hb_install"
  rm -rf $install_dir
  mkdir -p $install_dir
  while read -r package; do
    read -r package_name op package_version <<< $(_decipher_pip_package_entry "${package}")
    echo "      Searching for ${package_name}:${package_version}" >&2
    conda search --override-channels -c conda-forge -c defaults --json "${package_name}" | jq -r --arg pkg "${package_name}" --arg ver "${package_version}" '
      if (.[$pkg] == null or (.[$pkg] | map(has("version")) | all) == false) then
        empty
      else
        .[$pkg][] | select(has("version")) | .version as $version
        | ($version | split(".") | map(tonumber)) as $arrayed_version
        | ($ver | split(".") | map(tonumber)) as $arrayed_ver
        | if $arrayed_version >= $arrayed_ver then
            $pkg + "==" + $version
          else
            empty
          end
        | halt_error
      end
    ' 2>> $install_dir/conda_package_list.txt
    echo >> $install_dir/conda_package_list.txt
  done < setup/pip_packages.txt
  grep -v -f <(cut -d '=' -f1 $install_dir/conda_package_list.txt) setup/pip_packages.txt 2> $install_dir/updated_pip_packages.txt
}

_update_package_version() {
  local temp_file="$1"
  local package="$2"
  local version="$3"
  local upper_version="$4"

  awk -v pkg="$package" -v ver="$version" -v upper_ver="$upper_version" '
  {
    if ($0 ~ "(^[[:space:]-]+)" pkg "([[:space:]=<>]+|$)" && $0 !~ ":") { # If the line contains the package name
      pre = substr($0, 1, index($0, pkg) - 1)  # Capture the beginning of the line
      #sub("([[:space:]-])" pkg "([>=<[:space:]][0-9\.a-z]*)?", "\1")  # Remove existing version constraints
      #post = substr($0, index($0, pkg) + length(pkg))  # Capture the rest of the line

      if (pkg && ver && upper_ver) {
        print pre pkg ">=" ver ",<" upper_ver
      } else if (pkg && ver) {
        print pre pkg ">=" ver
      } else {
        print $0 # Print line unchanged
      }
    } else {
      print $0  # Print line unchanged
    }
  }' "$temp_file" > "${temp_file}.tmp" && mv "${temp_file}.tmp" "$temp_file"
}

update_environment_yml() {
  local env_file="$1"
  local export_file="$2"
  local updated_file="$3"

  local install_dir="/tmp/hb_install"
  rm -rf $install_dir
  mkdir -p $install_dir

  local temp_file="$install_dir/env.yml"

  cat "$env_file" > "$temp_file"
  cp "$env_file" /tmp/tmp.yml

  # Loop over the dependencies in the exported environment
  grep -P "^\s+-" "$env_file" | grep -v ":" | while read -r line; do
    # Extract the package name and version
    local package=$(echo "$line" | sed -e 's/^ *- *//' | awk -F "[<>=]" '{print $1}' | xargs)
    local upper_version=$(echo "$line" | awk -F "<" '{print $2}' | xargs)

    # Grab the latest version of the package from conda
    local version=$(grep -P "^\s+-\s+${package}=" "$export_file" | grep -v : | tail -n1 | awk -F "[=]" '{print $2}' | xargs)

    if [ -z "${version}" ]; then
      # echo "No version found for ${package}" >&2
      continue
    fi

    # Update the version in the environment.yml file
    _update_package_version "$temp_file" "${package}" "${version}" "${upper_version}"
  done

  mv "$temp_file" "$updated_file"
  echo >&2
  echo "Updated $updated_file with installed versions" >&2
}

# Check if the script is being sourced
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  return 0
fi

if [ "$1" == "--test" ]; then
  test_function=$2
  $test_function "${@:3}"
  exit 0
fi
