# AetherOS: start the Sway desktop when logging in on the first text console.
# Exiting Sway returns to the shell (no restart loop). To opt out:
#   mkdir -p ~/.config/aether && touch ~/.config/aether/no-autostart
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=sway
export XDG_SESSION_DESKTOP=sway
export MOZ_ENABLE_WAYLAND=1
export WLR_RENDERER="${WLR_RENDERER:-pixman}"
export WLR_NO_HARDWARE_CURSORS="${WLR_NO_HARDWARE_CURSORS:-1}"
case "$-" in
    *i*)
        if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ] \
            && [ "$(tty 2>/dev/null)" = "/dev/tty1" ] \
            && [ ! -e "${HOME:-/nonexistent}/.config/aether/no-autostart" ] \
            && command -v sway >/dev/null 2>&1; then
            sway
        fi
        ;;
esac
