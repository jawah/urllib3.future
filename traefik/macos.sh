#!/usr/bin/env sh
# MacOS in GitHub Actions does not ship with Docker due to legal reasons
# we have to circumvent that limitation by using Colima as a viable alternative.
set -e
sudo security authorizationdb write com.apple.trust-settings.admin allow
brew install docker
brew install docker-compose
brew install colima
colima start --network-address
colima list
