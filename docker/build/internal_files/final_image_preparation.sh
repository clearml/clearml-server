#!/usr/bin/env bash
set -x
set -o errexit
set -o nounset
set -o pipefail

apt-get update -y
apt-get install -y python3-setuptools python3-dev build-essential nginx gettext vim curl

python3 -m ensurepip
python3 -m pip install --upgrade pip
python3 -m pip install -r /opt/clearml/apiserver/requirements.txt
mkdir -p /opt/clearml/log
mkdir -p /opt/clearml/config
ln -svf /dev/stdout /var/log/nginx/access.log
ln -svf /dev/stderr /var/log/nginx/error.log
mv /tmp/internal_files/clearml.conf.template /etc/nginx/clearml.conf.template
mv /tmp/internal_files/clearml_subpath.conf.template /etc/nginx/clearml_subpath.conf.template

rm -d -r "$(pip cache dir)"
apt-get clean
