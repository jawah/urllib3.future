#!/usr/bin/env sh
# MacOS in GitHub Actions does not ship with Docker due to legal reasons
# we have to circumvent that limitation by using Colima as a viable alternative.
set -e
sudo security authorizationdb write com.apple.trust-settings.admin allow
brew install docker
docker --version
brew install docker-compose
docker compose version
brew install colima
sudo curl -L -o /opt/homebrew/Cellar/lima/0.22.0/bin/limactl https://github.com/mikekazakov/lima-nohvf/raw/master/limactl && sudo chmod +x /opt/homebrew/Cellar/lima/0.22.0/bin/limactl
colima start --network-address --arch arm64 --vm-type=qemu
colima list
