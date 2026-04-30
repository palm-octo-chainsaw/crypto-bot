#!/bin/bash

VERSION=$(git describe --tags --always 2>/dev/null || echo unknown)
export VERSION
docker compose build --build-arg VERSION="$VERSION"
docker compose up -d