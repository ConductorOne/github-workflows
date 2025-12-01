#!/bin/bash

# Function to sleep if SLEEP is configured
sleep_if_configured() {
  if [ -n "${SLEEP:-}" ] && [ "$SLEEP" != "" ]; then
    echo "Sleeping for ${SLEEP} seconds..."
    sleep "$SLEEP"
  fi
}

