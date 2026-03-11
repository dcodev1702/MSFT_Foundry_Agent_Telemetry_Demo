#!/usr/bin/env bash

resolve_bot_secret() {
    local suffix="${BOT_SECRET_SUFFIX:-botprd}"
    local secret_name="${BOT_SECRET_NAME:-bot-app-client-secret}"
    local vault_name="${BOT_SECRET_KEYVAULT_NAME:-zolabbotkv${suffix}}"
    local secret_value=""

    if [[ -n "${BOT_SECRET:-}" ]]; then
        BOT_SECRET_RESOLUTION='BOT_SECRET environment variable'
        printf '%s\n' "${BOT_SECRET}"
        return 0
    fi

    if secret_value="$(az keyvault secret show --vault-name "${vault_name}" --name "${secret_name}" --query value -o tsv 2>/dev/null)" && [[ -n "${secret_value}" ]]; then
        BOT_SECRET_RESOLUTION="Azure Key Vault secret ${secret_name} in ${vault_name}"
        printf '%s\n' "${secret_value}"
        return 0
    fi

    echo "ERROR: Unable to resolve the bot app secret. Set BOT_SECRET explicitly or ensure Key Vault '${vault_name}' contains secret '${secret_name}'." >&2
    return 1
}