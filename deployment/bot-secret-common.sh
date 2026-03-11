#!/usr/bin/env bash

bot_secret_vault_name() {
    local suffix="${BOT_SECRET_SUFFIX:-botprd}"
    printf '%s\n' "${BOT_SECRET_KEYVAULT_NAME:-zolabbotkv${suffix}}"
}

bot_secret_name() {
    printf '%s\n' "${BOT_SECRET_NAME:-bot-app-client-secret}"
}

resolve_bot_secret_source() {
    local vault_name
    local secret_name

    vault_name="$(bot_secret_vault_name)"
    secret_name="$(bot_secret_name)"

    if [[ "${BOT_SECRET_OVERRIDE_PRESENT:-0}" == "1" ]]; then
        printf '%s\n' 'BOT_SECRET environment variable'
        return 0
    fi

    printf '%s\n' "Azure Key Vault secret ${secret_name} in ${vault_name}"
}

resolve_bot_secret() {
    local vault_name
    local secret_name
    local secret_value=""

    vault_name="$(bot_secret_vault_name)"
    secret_name="$(bot_secret_name)"

    if [[ -n "${BOT_SECRET:-}" ]]; then
        printf '%s\n' "${BOT_SECRET}"
        return 0
    fi

    if secret_value="$(az keyvault secret show --vault-name "${vault_name}" --name "${secret_name}" --query value -o tsv 2>/dev/null)" && [[ -n "${secret_value}" ]]; then
        printf '%s\n' "${secret_value}"
        return 0
    fi

    echo "ERROR: Unable to resolve the bot app secret. Set BOT_SECRET explicitly or ensure Key Vault '${vault_name}' contains secret '${secret_name}'." >&2
    return 1
}