#!/usr/bin/env python3
"""Shell completion command helpers."""

import sys

from .common import err
def cmd_completion(args):
    shell = args.shell
    if shell == "bash":
        print(_completion_bash())
    elif shell == "zsh":
        print(_completion_zsh())
    elif shell == "fish":
        print(_completion_fish())
    else:
        err(f"Unsupported shell: {shell}")
        sys.exit(2)


def _completion_bash() -> str:
    return r'''_apsta_complete() {
    local cur prev
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local commands="detect start stop status profile config enable disable scan-usb recommend completion"

    if [[ ${COMP_CWORD} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
    return 0
    fi

    case "${COMP_WORDS[1]}" in
    start)
        COMPREPLY=( $(compgen -W "--force --json" -- "${cur}") )
        ;;
    detect|status)
        COMPREPLY=( $(compgen -W "--json" -- "${cur}") )
        ;;
    config)
        COMPREPLY=( $(compgen -W "--set" -- "${cur}") )
        ;;
    profile)
        if [[ ${COMP_CWORD} -eq 2 ]]; then
            COMPREPLY=( $(compgen -W "list show use create delete" -- "${cur}") )
        fi
        ;;
    completion)
        COMPREPLY=( $(compgen -W "bash zsh fish" -- "${cur}") )
        ;;
    esac
}

complete -F _apsta_complete apsta'''


def _completion_zsh() -> str:
    return r'''#compdef apsta

_apsta() {
    local -a commands
    commands=(
        'detect:Detect hardware AP+STA capability'
        'start:Start hotspot'
        'stop:Stop hotspot'
        'status:Show status'
        'profile:Manage named hotspot profiles'
        'config:View/edit config'
        'enable:Install auto-start hooks'
        'disable:Disable auto-start hooks'
        'scan-usb:Detect USB WiFi adapters'
        'recommend:Suggest USB adapters to buy'
        'completion:Print shell completion script'
    )

    _arguments -C \
        '1:command:->cmds' \
        '*::arg:->args'

    case $state in
        cmds)
            _describe 'command' commands
            ;;
        args)
            case $words[2] in
                start)
                    _values 'options' --force --json
                    ;;
                detect|status)
                    _values 'options' --json
                    ;;
                config)
                    _values 'options' --set
                    ;;
                profile)
                    _values 'action' list show use create delete
                    ;;
                completion)
                    _values 'shell' bash zsh fish
                    ;;
            esac
            ;;
    esac
}

_apsta "$@"'''


def _completion_fish() -> str:
    return r'''complete -c apsta -f
complete -c apsta -n "__fish_use_subcommand" -a "detect" -d "Detect hardware AP+STA capability"
complete -c apsta -n "__fish_use_subcommand" -a "start" -d "Start hotspot"
complete -c apsta -n "__fish_use_subcommand" -a "stop" -d "Stop hotspot"
complete -c apsta -n "__fish_use_subcommand" -a "status" -d "Show status"
complete -c apsta -n "__fish_use_subcommand" -a "profile" -d "Manage named hotspot profiles"
complete -c apsta -n "__fish_use_subcommand" -a "config" -d "View/edit config"
complete -c apsta -n "__fish_use_subcommand" -a "enable" -d "Install auto-start hooks"
complete -c apsta -n "__fish_use_subcommand" -a "disable" -d "Disable auto-start hooks"
complete -c apsta -n "__fish_use_subcommand" -a "scan-usb" -d "Detect USB WiFi adapters"
complete -c apsta -n "__fish_use_subcommand" -a "recommend" -d "Suggest USB adapters"
complete -c apsta -n "__fish_use_subcommand" -a "completion" -d "Print shell completion script"

complete -c apsta -n "__fish_seen_subcommand_from start" -l force -d "Force single-interface mode"
complete -c apsta -n "__fish_seen_subcommand_from start detect status" -l json -d "Output JSON"
complete -c apsta -n "__fish_seen_subcommand_from profile" -a "list show use create delete"
complete -c apsta -n "__fish_seen_subcommand_from config" -l set -d "Set config key"
complete -c apsta -n "__fish_seen_subcommand_from completion" -a "bash zsh fish"'''


