#!/usr/bin/env bash

set -o errexit

function error() {
    echo $@ 1>&2
}

# Check OS
OS=$(awk -F= '/^ID=/ { print $2 }' /etc/os-release | tr -d '"')
if [[ ! ${OS} =~ ^(centos)$ ]] && [[ ! ${OS} =~ ^(rhel)$ ]] && [[ ! ${OS} =~ ^(rocky)$ ]] && [[ ! ${OS} =~ ^(almalinux)$ ]]; then
    error "\"${OS}\" is an unsupported OS. We support only CentOS, RHEL, Rocky or AlmaLinux."
    error "It's not recommended to run the code in this repo locally on your personal machine, as it makes some opinionated configuration changes to the machine it's running on"
    exit 1
fi

#Check CentOS version
VER=$(awk -F= '/^VERSION_ID=/ { print $2 }' /etc/os-release | tr -d '"' | cut -f1 -d'.')
VER_SUPPORTED=8

if [[ ${OS} =~ ^(centos)$ && ${VER} -ne ${VER_SUPPORTED} ]]; then
    error "CentOS version ${VER_SUPPORTED} is required."
    exit 1
elif [[ ${OS} =~ ^(rhel)$ && ${VER} -ne ${VER_SUPPORTED} ]]; then
    error "RHEL version ${VER_SUPPORTED} is required."
    exit 1
fi

echo "Installing environment"
scripts/install_environment.sh
echo "Done installing"

echo "Creating image"
make bring_assisted_service
make image_build
echo "Done creating image"

echo "Installing minikube and oc"
scripts/install_minikube.sh
echo "Done installing minikube and oc"

if [ -z "${NO_MINIKUBE}" ]; then
    echo "Install and start minikube"
    make start_minikube
fi