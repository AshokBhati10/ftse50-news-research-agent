#!/bin/sh
# Create research-stack/.env.rabbitmq (RabbitMQ credentials) if it does not exist yet.
# The file is git-ignored (.env.*) and mode 600; the password is never printed.
# This is separate from research-stack/.env so that file is left untouched.
set -eu
cd "$(dirname "$0")/.."
if [ -f .env.rabbitmq ]; then
  echo ".env.rabbitmq already exists - leaving it unchanged"
  exit 0
fi
umask 077
printf 'RABBITMQ_DEFAULT_USER=research\nRABBITMQ_DEFAULT_PASS=%s\n' "$(openssl rand -hex 24)" > .env.rabbitmq
echo "created research-stack/.env.rabbitmq (mode 600)"
