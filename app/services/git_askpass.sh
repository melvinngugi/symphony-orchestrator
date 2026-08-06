#!/bin/sh

case "$1" in
    *sername*)
        printf '%s\n' "$SYMPHONY_GIT_USERNAME"
        ;;
    *assword*)
        printf '%s\n' "$SYMPHONY_GIT_PASSWORD"
        ;;
    *)
        exit 1
        ;;
esac
