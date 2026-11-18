"""Single backend factory for QQ, dual-account loops and standalone actors."""
from pathlib import Path


def create_ai_client(config, *, system_prompt=None, sessions_file="data/sessions.json", account_key=""):
    prompt = config.bot.system_prompt if system_prompt is None else system_prompt
    if config.model_backend.backend == "deepseek_harness":
        from dsh_client import DeepSeekHarnessClient
        path = Path(sessions_file)
        # Backend ids are never interpreted as Antigravity conversation ids.
        return DeepSeekHarnessClient(config.deepseek_harness, system_prompt=prompt,
            sessions_file=str(path.with_name(path.stem + "_dsh" + path.suffix)), account_key=account_key)
    from antigravity_client import AntigravityClient
    return AntigravityClient(**config.antigravity.model_dump(), system_prompt=prompt,
        sessions_file=sessions_file, account_key=account_key)
