"""Public, credential-free view of installed agents for the local UI."""

import copy


def _auth_view(auth, installed=True):
    auth = auth if isinstance(auth, dict) else {}
    kind = auth.get("kind") or "unknown"
    authenticated = bool(auth.get("authenticated", True))
    credential_env = auth.get("credential_env")
    source = auth.get("source")
    stored = bool(auth.get("stored"))
    masked = auth.get("masked")
    message = str(auth.get("message") or "")
    if not installed:
        state, label = "unavailable", "未安装"
    elif authenticated:
        state = "authenticated"
        label = "已登录" if kind == "account" else "已配置" if kind == "api_key" else "可用"
    elif kind == "account":
        state, label = "signed_out", "未登录"
    elif credential_env:
        state, label = "missing_key", "缺少密钥"
    else:
        state, label = "unknown", "待检测"
    return {
        "kind": kind,
        "authenticated": authenticated,
        "credential_env": credential_env if isinstance(credential_env, str) else None,
        "source": source if isinstance(source, str) else None,
        "stored": stored,
        "masked": masked if isinstance(masked, str) else None,
        "message": message,
        "state": state,
        "label": label,
    }


def _agent_auth(entry):
    providers = entry.get("providers") or []
    if not entry.get("installed"):
        return {
            "state": "not_installed",
            "label": "未安装",
            "message": entry.get("message") or "未检测到本机安装",
            "authenticated": False,
            "ready_providers": 0,
            "provider_count": 0,
        }
    if not providers:
        return {
            "state": "no_models",
            "label": "未发现模型",
            "message": entry.get("message") or "已安装，但未发现可用模型",
            "authenticated": False,
            "ready_providers": 0,
            "provider_count": 0,
        }
    ready = [p for p in providers if (p.get("auth") or {}).get("authenticated")]
    missing = [p for p in providers if (p.get("auth") or {}).get("state") == "missing_key"]
    signed_out = [p for p in providers if (p.get("auth") or {}).get("state") == "signed_out"]
    if ready:
        return {
            "state": "ready",
            "label": "可用",
            "message": f"{len(ready)} / {len(providers)} 个供应商已就绪",
            "authenticated": True,
            "ready_providers": len(ready),
            "provider_count": len(providers),
        }
    if missing:
        return {
            "state": "missing_key",
            "label": "缺少密钥",
            "message": "请为需要 API Key 的供应商配置密钥",
            "authenticated": False,
            "ready_providers": 0,
            "provider_count": len(providers),
        }
    if signed_out:
        return {
            "state": "signed_out",
            "label": "需要登录",
            "message": "请先在客户端登录账号，然后重新扫描",
            "authenticated": False,
            "ready_providers": 0,
            "provider_count": len(providers),
        }
    return {
        "state": "unknown",
        "label": "待检测",
        "message": "尚未检测到可用的登录状态",
        "authenticated": False,
        "ready_providers": 0,
        "provider_count": len(providers),
    }


def build_inventory(catalogue, *, discovering=False, scanned_at=None):
    """Return a copy suitable for the browser; never include executable settings."""
    agents = []
    for entry in copy.deepcopy(catalogue or []):
        installed = bool(entry.get("installed"))
        providers = []
        for provider in entry.get("providers", []):
            provider["auth"] = _auth_view(provider.get("auth"), installed)
            provider["authenticated"] = provider["auth"]["authenticated"]
            providers.append(provider)
        entry["providers"] = providers
        entry["installed"] = installed
        entry["auth"] = _agent_auth(entry)
        agents.append(entry)
    return {
        "agents": agents,
        "discovering": bool(discovering),
        "scanned_at": scanned_at,
    }
