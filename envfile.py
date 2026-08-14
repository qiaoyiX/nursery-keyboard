"""Read a systemd EnvironmentFile the way systemd does.

Only the manual migration scripts use this, and deliberately so. The services get
their environment from systemd itself, and `nursery-tracker` / `nursery-sleep-monitor`
must never see DATABASE_URL at all (ADR-001: live reads keep Neon's compute awake and
burn ~180 free-tier hours/month). Importing this from storage.py would hand every
service the credentials by accident, which is exactly the failure the split exists to
prevent — so the fallback is opt-in, per script.

The format is KEY=value, one per line, with `#` comments and blank lines. That is not
shell syntax: `cat backup.env | xargs` feeds the comment lines to the command as
arguments (`env: '#': No such file or directory`), and `. backup.env` would execute a
`&` in a connection string as a control operator.
"""
import os


def load_env_file(path, override=False):
    """Set os.environ from `path`. Returns the keys applied.

    Silently returns nothing if the file is absent or unreadable — an unconfigured
    or root-only backup file is a normal state for a script that also accepts the
    variable straight from the environment.
    """
    applied = []
    try:
        with open(path) as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return applied

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # systemd strips one layer of matching quotes; a Neon password may legally
        # contain the characters that would otherwise need them.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied
