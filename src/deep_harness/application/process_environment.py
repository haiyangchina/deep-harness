"""Keep native runtime locations without inheriting provider or profile credentials."""

import os

SYSTEM_VARIABLES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TEMP",
        "TMP",
        "TMPDIR",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "OS",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "USERDOMAIN",
        "USERDOMAIN_ROAMINGPROFILE",
        "LOGONSERVER",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "ALLUSERSPROFILE",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
    }
)


def command_environment() -> dict[str, str]:
    # Native Windows/MSYS tools need the OS user/location context to initialize.
    # An allowlist still excludes model keys, Harness_HOME and arbitrary application tokens.
    return {key: value for key, value in os.environ.items() if key.upper() in SYSTEM_VARIABLES}
