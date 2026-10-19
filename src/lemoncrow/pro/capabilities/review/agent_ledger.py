"""Authoring-agent side-effect ledger (Phase 9R item 17).

While an agent develops a Change it can act outside the repository: install
packages, call networks/APIs and cloud CLIs, run database commands, read
secrets, push to remotes, or write files outside the workspace. None of that is
in the diff. This module classifies one recorded command (or out-of-repo file
write) into a typed ledger class; ``development_ingest`` records consequential
ones as ``side_effect`` Development events, which Activity and the Brief show.

Deterministic and conservative: a command is split on shell operators and its
most severe segment wins. Benign in-workspace work is ``None``: lockfile
restores, loopback network calls, temp-directory writes and ordinary reads,
builds and tests. Command text is data; it is never executed or expanded.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Final, Literal

LedgerCategory = Literal[
    "secret_access", "git_push", "cloud", "database", "outside_write", "package_install", "network"
]
LEDGER_CATEGORIES: Final[tuple[str, ...]] = (
    "secret_access",
    "git_push",
    "cloud",
    "database",
    "outside_write",
    "package_install",
    "network",
)
"""Most severe first; a compound command reports its most severe segment."""

LEDGER_LABELS: Final[dict[str, str]] = {
    "secret_access": "Secret access",
    "git_push": "Git push",
    "cloud": "Cloud CLI",
    "database": "Database command",
    "outside_write": "Write outside the workspace",
    "package_install": "Package install",
    "network": "Network call",
}
_RANK = {name: index for index, name in enumerate(LEDGER_CATEGORIES)}


@dataclass(frozen=True, slots=True)
class LedgerClass:
    category: LedgerCategory
    label: str

    @property
    def title(self) -> str:
        return LEDGER_LABELS[self.category]


_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WRAPPERS = frozenset(
    {"sudo", "env", "time", "nohup", "exec", "command", "builtin", "doas", "nice", "timeout", "xargs", "stdbuf"}
    | {"corepack"}
)
# Wrapper options whose value is the next word (P12-AGENT): without them
# ``sudo -u root git push`` would read ``root`` as the command it runs.
_WRAPPER_VALUE_FLAGS: Final[dict[str, frozenset[str]]] = {
    "sudo": frozenset(
        {"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-U", "-D", "-R", "-T", "--user", "--group", "--host"}
        | {"--prompt", "--close-from", "--role", "--type", "--other-user", "--chdir", "--chroot"}
        | {"--command-timeout"}
    ),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "--unset", "--chdir"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "xargs": frozenset(
        {"-I", "-L", "-n", "-P", "-s", "-d", "-E", "-a", "--arg-file", "--delimiter", "--max-args", "--max-lines"}
        | {"--max-procs", "--max-chars", "--eof", "--process-slot-var"}
    ),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "exec": frozenset({"-a"}),
}
_ENV_SPLIT_FLAGS = frozenset({"-S", "--split-string"})
_TIMEOUT_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"})
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
_SHELL_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "fi", "done", "esac", "coproc"}
)
_FUNCTION_HEAD = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\(\)$")
XARGS_INPUT: Final[str] = "{xargs-input}"
"""Stands for the arguments xargs appends from its input (never a flag or a local path)."""
MAX_SHELL_DEPTH: Final[int] = 4
"""Nested command text (``sh -c``, ``eval``, ``$(...)``) read this deep; deeper is unclassifiable."""
_LOOPBACK = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]*@)?(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\]|::1|"
    r"host\.docker\.internal)(?::\d+)?(?:[/?#].*)?$",
    re.IGNORECASE,
)
_URLISH = re.compile(r"^(?:[a-z][a-z0-9+.-]*://\S+|[\w.-]+\.[a-z]{2,}(?::\d+)?(?:/\S*)?|[\w.-]+@[\w.-]+:\S*)$", re.I)
_TEMP_ROOTS = ("/tmp/", "/var/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/", "/dev/shm/")
_DEVICE_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
_SECRET_PATHS = re.compile(
    r"(?:^|/)(?:\.aws/(?:credentials|config)|\.ssh/(?!known_hosts)[^/\s]+|\.netrc|\.pgpass|\.npmrc|\.pypirc|"
    r"\.docker/config\.json|\.kube/config|\.config/gh/hosts\.ya?ml|\.git-credentials|\.gnupg/|"
    r"\.config/gcloud/|\.azure/|id_(?:rsa|ed25519|ecdsa)(?:\.pub)?)$|(?:^|/)\.env(?:\.(?!example|sample|template|dist)[\w.-]+)?$"
)
_SECRET_ENV = re.compile(r"\$\{?[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIALS?)\b")
_READERS = frozenset({"cat", "less", "more", "head", "tail", "bat", "grep", "rg", "awk", "sed", "cp", "base64", "xxd"})
_FILE_WRITERS = frozenset({"cp", "mv", "install", "ln", "rm", "mkdir", "touch", "chmod", "chown", "rsync", "tee", "dd"})

_INSTALL_VERBS: Final[dict[str, frozenset[str]]] = {
    "pip": frozenset({"install", "download"}),
    "pip3": frozenset({"install", "download"}),
    "pipx": frozenset({"install", "run", "inject"}),
    "uv": frozenset({"add", "sync"}),
    "poetry": frozenset({"add", "install", "sync"}),
    "pdm": frozenset({"add", "install", "sync"}),
    "pipenv": frozenset({"install", "sync"}),
    "conda": frozenset({"install", "create"}),
    "mamba": frozenset({"install", "create"}),
    "npm": frozenset({"install", "i", "add", "ci"}),
    "pnpm": frozenset({"add", "install", "i", "dlx", "fetch"}),
    "yarn": frozenset({"add", "dlx", "install"}),
    "bun": frozenset({"add", "install", "i", "x"}),
    "cargo": frozenset({"install", "add", "fetch", "vendor"}),
    "go": frozenset({"install", "get"}),
    "gem": frozenset({"install"}),
    "bundle": frozenset({"install"}),
    "brew": frozenset({"install", "tap", "upgrade"}),
    "apt": frozenset({"install"}),
    "apt-get": frozenset({"install"}),
    "yum": frozenset({"install"}),
    "dnf": frozenset({"install"}),
    "apk": frozenset({"add"}),
    "pacman": frozenset({"-S", "-Sy", "-Syu"}),
    "composer": frozenset({"require", "global", "install"}),
    "dotnet": frozenset({"add", "tool", "restore"}),
    "choco": frozenset({"install"}),
    "winget": frozenset({"install"}),
    "snap": frozenset({"install"}),
}
# Upgrades resolve new versions from the registry even without naming a package.
_UPGRADE_VERBS: Final[dict[str, frozenset[str]]] = {
    "npm": frozenset({"update", "upgrade", "up"}),
    "pnpm": frozenset({"update", "upgrade", "up"}),
    "yarn": frozenset({"upgrade", "up", "upgrade-interactive"}),
    "bun": frozenset({"update", "upgrade"}),
    "poetry": frozenset({"update"}),
    "pdm": frozenset({"update"}),
    "cargo": frozenset({"update"}),
    "composer": frozenset({"update", "upgrade"}),
    "bundle": frozenset({"update"}),
    "gem": frozenset({"update"}),
    "conda": frozenset({"update", "upgrade"}),
    "mamba": frozenset({"update", "upgrade"}),
    "apt": frozenset({"upgrade", "full-upgrade", "dist-upgrade"}),
    "apt-get": frozenset({"upgrade", "dist-upgrade"}),
    "dnf": frozenset({"upgrade", "update"}),
    "yum": frozenset({"upgrade", "update"}),
}
# ``npx tsc`` runs the workspace's own binary; only an explicit fetch counts.
_RUNNERS = frozenset({"npx", "bunx", "pnpx"})
_FETCH_FLAGS = frozenset({"-y", "--yes", "-p", "--package"})
_NETWORK_TOOLS = frozenset({"curl", "wget", "http", "https", "httpie", "xh", "nc", "ncat", "telnet", "ftp", "sftp"})
_REMOTE_SHELLS = frozenset({"ssh", "scp", "mosh"})
_CLOUD_TOOLS = frozenset(
    {
        "aws",
        "gcloud",
        "gsutil",
        "bq",
        "az",
        "kubectl",
        "helm",
        "terraform",
        "tofu",
        "pulumi",
        "doctl",
        "flyctl",
        "fly",
        "heroku",
        "vercel",
        "netlify",
        "firebase",
        "wrangler",
        "serverless",
        "sls",
        "eksctl",
        "oci",
        "ibmcloud",
        "railway",
        "render",
        "supabase",
        "cdk",
        "sam",
        "ansible-playbook",
    }
)
_DATABASE_TOOLS = frozenset(
    {
        "psql",
        "pg_dump",
        "pg_restore",
        "pg_dumpall",
        "dropdb",
        "createdb",
        "mysql",
        "mysqldump",
        "mariadb",
        "mongo",
        "mongosh",
        "mongodump",
        "mongorestore",
        "redis-cli",
        "cqlsh",
        "clickhouse-client",
        "sqlcmd",
        "influx",
        "cockroach",
        "flyway",
        "liquibase",
    }
)
_MIGRATION_COMMANDS = re.compile(
    r"\b(?:alembic\s+(?:upgrade|downgrade|stamp)|prisma\s+(?:migrate\s+(?:deploy|reset|dev)|db\s+(?:push|execute|seed))|"
    r"(?:rails|rake)\s+db:\w+|manage\.py\s+(?:migrate|flush|dbshell|loaddata)|knex\s+migrate|sequelize\s+db:\w+|"
    r"diesel\s+(?:migration\s+run|database\s+reset)|sqlx\s+migrate\s+run|atlas\s+(?:migrate\s+apply|schema\s+apply)|"
    r"drizzle-kit\s+(?:push|migrate)|goose\s+(?:up|down|reset)|dbmate\s+(?:up|down|migrate|drop))\b"
)
_SECRET_TOOLS = re.compile(
    r"^(?:security\s+find-(?:generic|internet)-password|pass\s+(?:show|\S+$)|op\s+(?:read|item\s+get|inject)|"
    r"vault\s+(?:read|kv\s+get|login)|gh\s+auth\s+(?:token|status\s+--show-token)|"
    r"aws\s+secretsmanager\s+get-secret-value|aws\s+ssm\s+get-parameters?\b.*--with-decryption|"
    r"gcloud\s+secrets\s+versions\s+access|az\s+keyvault\s+secret\s+show|kubectl\s+get\s+secrets?\b|"
    r"doppler\s+secrets|infisical\s+secrets|sops\s+(?:-d|--decrypt))"
)


# --------------------------------------------------------------------------- #
# Commands whose effect lives in a file or another interpreter (RC12-E)        #
# --------------------------------------------------------------------------- #
#
# ``bash deploy.sh``, ``source env.sh``, ``./run``, ``make``, ``npm run x``,
# ``python tool.py``, ``node app.js`` run text this classifier cannot see in the
# command line. The only copy of that text available when an agent action is
# gated is the agent's own mutable worktree, which the agent can rewrite between
# the approval and the run, so it is never read (and nothing is executed): the
# command is *unresolved*, which approval counts as every configured class.

_SCRIPT_EXTENSIONS: Final[tuple[str, ...]] = (
    *(".sh", ".bash", ".zsh", ".fish", ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx"),
    *(".rb", ".pl", ".php", ".lua", ".ps1", ".bat", ".cmd", ".r", ".jl", ".cmake", ".java", ".groovy", ".kts"),
    *(".swift", ".dart", ".zig", ".awk", ".exs", ".scala", ".tcl"),
)
# Only root-owned system directories: /usr/local/bin and Homebrew's bin are user-writable.
_SYSTEM_BIN_DIRS: Final[tuple[str, ...]] = ("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/")
# Programs that run the recipes, scripts or plugins of a file in the repository, whatever the arguments.
_TASK_RUNNERS: Final[frozenset[str]] = frozenset(
    {"make", "gmake", "bmake", "just", "task", "rake", "ninja", "samu", "turbo", "nx", "lerna", "rush", "gulp"}
    | {"grunt", "gradle", "gradlew", "mvn", "mvnw", "ant", "sbt", "tox", "nox", "mise", "hatch", "pixi", "cmake"}
    | {"ctest", "meson", "pre-commit", "invoke", "fab", "doit"}
)
# Wrappers that run another command whose own flags this classifier does not parse: any use with arguments.
_UNPARSED_WRAPPERS: Final[frozenset[str]] = frozenset(
    {"setsid", "ionice", "flock", "strace", "ltrace", "watch", "script", "xvfb-run", "runuser", "pkexec", "chroot"}
    | {"unshare", "nsenter", "firejail", "valgrind", "gdb", "lldb", "perf", "taskset", "chrt", "systemd-run"}
    | {"unbuffer", "faketime", "eatmydata", "cpulimit", "sg", "newgrp", "busybox", "watchexec", "entr", "bwrap"}
    | {"fakeroot", "proot", "dbus-run-session", "caffeinate", "at", "batch", "limactl"}
)
# Programs whose named subcommand runs repository or user code.
_RUNNER_VERBS: Final[dict[str, frozenset[str]]] = {
    "bundle": frozenset({"exec"}),
    "conda": frozenset({"run"}),
    "mamba": frozenset({"run"}),
    "micromamba": frozenset({"run"}),
    "direnv": frozenset({"exec"}),
    "bazel": frozenset({"run"}),
    "bazelisk": frozenset({"run"}),
    "buck2": frozenset({"run"}),
    "flutter": frozenset({"run"}),
}
_PACKAGE_SCRIPT_VERBS: Final[frozenset[str]] = frozenset(
    {"run", "run-script", "rum", "urn", "test", "tst", "t", "start", "stop", "restart", "exec", "x", "create", "init"}
    | {"it", "cit", "install-test", "install-ci-test", "ist", "rebuild", "rb"}
)
_NPM_BUILTINS: Final[frozenset[str]] = frozenset(
    {"install", "i", "add", "ci", "uninstall", "remove", "rm", "r", "un", "unlink", "update", "up", "upgrade", "ls"}
    | {"list", "ll", "la", "outdated", "audit", "fund", "pack", "publish", "unpublish", "view", "v", "info", "show"}
    | {"search", "config", "c", "get", "set", "login", "logout", "whoami", "ping", "cache", "dedupe", "find-dupes"}
    | {"prune", "link", "ln", "root", "bin", "prefix", "version", "help", "h", "docs", "home", "repo", "bugs"}
    | {"explain", "why", "diff", "doctor", "completion", "deprecate", "dist-tag", "owner", "team", "token"}
    | {"access", "org", "profile", "hook", "sbom", "stars", "star", "unstar", "adduser", "query", "pkg"}
)
_PNPM_BUILTINS: Final[frozenset[str]] = frozenset(
    {"add", "install", "i", "update", "up", "upgrade", "remove", "rm", "uninstall", "un", "link", "ln", "unlink"}
    | {"list", "ls", "ll", "why", "outdated", "audit", "fetch", "prune", "store", "root", "bin", "config", "c"}
    | {"get", "set", "pack", "publish", "patch", "patch-commit", "patch-remove", "deploy", "dlx", "env", "import"}
    | {"licenses", "setup", "self-update", "doctor", "help", "dedupe", "approve-builds", "view", "info", "show"}
    | {"login", "logout", "whoami", "ping", "search", "access", "owner", "team", "dist-tag", "token"}
)
_YARN_BUILTINS: Final[frozenset[str]] = frozenset(
    {"install", "add", "remove", "up", "upgrade", "upgrade-interactive", "info", "why", "list", "config", "cache"}
    | {"link", "unlink", "version", "versions", "login", "logout", "publish", "pack", "outdated", "audit", "set"}
    | {"dlx", "workspaces", "help", "bin", "dedupe", "explain", "unplug", "constraints"}
)
_BUN_BUILTINS: Final[frozenset[str]] = frozenset(
    {"install", "i", "add", "a", "remove", "rm", "update", "upgrade", "pm", "init", "link", "unlink", "build"}
    | {"publish", "outdated", "audit", "patch", "help", "test"}
)
_DENO_BUILTINS: Final[frozenset[str]] = frozenset(
    {"test", "fmt", "lint", "check", "doc", "info", "bench", "types", "help", "upgrade", "completions", "coverage"}
)
_CARGO_SAFE: Final[frozenset[str]] = frozenset(
    {
        "build",
        "b",
        "test",
        "t",
        "check",
        "c",
        "clippy",
        "fmt",
        "doc",
        "d",
        "bench",
        "clean",
        "fetch",
        "tree",
        "metadata",
    }
    | {"update", "add", "remove", "rm", "install", "uninstall", "search", "publish", "package", "login", "logout"}
    | {"vendor", "init", "new", "version", "help", "fix", "rustc", "rustdoc", "locate-project", "pkgid", "yank"}
    | {"owner", "generate-lockfile", "verify-project", "report", "read-manifest", "info", "miri"}
)
_CARGO_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"--manifest-path", "--target-dir", "-p", "--package", "--features", "-F", "-Z", "--config", "--color"}
    | {"--profile", "--target", "-j", "--jobs", "--message-format", "--lockfile-path"}
)
_GO_SAFE: Final[frozenset[str]] = frozenset(
    {"build", "test", "vet", "fmt", "mod", "get", "install", "list", "env", "version", "clean", "doc", "fix", "work"}
    | {"help", "bug"}
)
_DOTNET_SAFE: Final[frozenset[str]] = frozenset(
    {"build", "test", "restore", "publish", "pack", "clean", "new", "add", "nuget", "format", "sln", "help"}
    | {"--version", "--info", "--list-sdks", "--list-runtimes", "list", "remove", "store", "tool"}
)
_GIT_VERBS: Final[frozenset[str]] = frozenset(
    {"add", "am", "apply", "archive", "bisect", "blame", "branch", "bundle", "checkout", "cherry-pick", "clean"}
    | {"clone", "commit", "config", "describe", "diff", "fetch", "format-patch", "gc", "grep", "init", "log"}
    | {"ls-files", "ls-remote", "ls-tree", "merge", "merge-base", "mv", "notes", "pull", "push", "range-diff"}
    | {"rebase", "reflog", "remote", "repack", "replace", "reset", "restore", "revert", "rev-list", "rev-parse"}
    | {"rm", "shortlog", "show", "show-ref", "sparse-checkout", "stash", "status", "submodule", "switch", "tag"}
    | {"worktree", "cat-file", "hash-object", "update-index", "update-ref", "symbolic-ref", "for-each-ref"}
    | {"count-objects", "fsck", "prune", "maintenance", "version", "help", "whatchanged", "name-rev", "cherry"}
    | {"check-ignore", "check-attr", "diff-tree", "diff-files", "diff-index", "var", "write-tree", "read-tree"}
    | {"commit-tree", "mktree", "unpack-file", "verify-commit", "verify-tag", "show-branch", "ls-tree", "mailinfo"}
)
_GIT_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--exec-path"}
)
# Configuration keys that name a program git runs.
_GIT_PROGRAM_KEYS: Final[frozenset[str]] = frozenset(
    {"core.pager", "core.editor", "core.sshcommand", "core.fsmonitor", "core.hookspath", "core.gitproxy"}
    | {"credential.helper", "diff.external", "gpg.program", "core.askpass", "sequence.editor", "gpg.ssh.program"}
    | {"uploadpack.packobjectshook", "protocol.ext.allow", "core.untrackedcache.program"}
)
_GIT_PROGRAM_FLAGS: Final[tuple[str, ...]] = ("--upload-pack", "--receive-pack", "--exec", "--ext-cmd", "-x", "-O")
_LAUNCHERS: Final[frozenset[str]] = frozenset({"uv", "poetry", "pdm", "pipenv", "rye"})
# Launcher options with an argument; any other option makes the launcher's command unresolved.
_LAUNCHER_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"--with", "--with-requirements", "--with-editable", "--python", "-p", "--project", "--directory", "--env-file"}
    | {"--extra", "--group", "--only-group", "--no-group", "--package", "--index", "--default-index", "--config-file"}
    | {"--color", "--cache-dir", "--python-preference", "--python-downloads", "--index-url", "--extra-index-url"}
    | {"--find-links", "-f", "--index-strategy", "--keyring-provider", "--resolution", "--prerelease", "-C", "-P"}
    | {"--exclude-newer", "--refresh-package", "--reinstall-package", "--upgrade-package", "--link-mode", "-c"}
    | {"--no-build-package", "--no-binary-package", "--allow-insecure-host", "--config", "--pypi-mirror"}
)
_LAUNCHER_BOOL_FLAGS: Final[frozenset[str]] = frozenset(
    {"-q", "--quiet", "-v", "-vv", "-vvv", "--verbose", "--no-cache", "-n", "--offline", "--no-progress"}
    | {"--native-tls", "--no-config", "--isolated", "--no-project", "--frozen", "--locked", "--no-sync", "--active"}
    | {"--no-dev", "--dev", "--all-extras", "--all-packages", "--exact", "--no-editable", "--refresh", "--reinstall"}
    | {"--no-env-file", "--managed-python", "--no-managed-python", "--no-python-downloads", "--preview", "--upgrade"}
    | {"-U", "--no-build", "--no-binary", "--no-ansi", "--ansi", "--no-interaction", "--no-plugins", "-V", "--version"}
    | {"-h", "--help", "--system-certs", "--no-install-project", "--no-install-workspace", "--no-install-local"}
)
MAX_LAUNCH_ROUNDS: Final[int] = 8
"""Wrappers and launchers (``sudo uv run env ...``) unwrapped this many times; deeper is unresolved."""
# Modules ``python -m`` runs as a known tool taking file arguments; any other module runs repository code.
_PYTHON_TOOL_MODULES: Final[frozenset[str]] = frozenset(
    {"pytest", "pip", "unittest", "ruff", "black", "mypy", "venv", "json.tool", "pyright", "flake8", "isort"}
    | {"compileall", "pylint", "ensurepip", "py_compile"}
)
_PYTHON = re.compile(r"^(?:python\d*(?:\.\d+)*|pypy\d*(?:\.\d+)*|ipython\d*|bpython)$")
# interpreter -> short flag letters that take inline program text, and long flags that do.
_NO_INLINE: Final[tuple[str, frozenset[str]]] = ("", frozenset())
_INLINE_PROGRAM: Final[dict[str, tuple[str, frozenset[str]]]] = {
    "node": ("ep", frozenset({"--eval", "--print"})),
    "nodejs": ("ep", frozenset({"--eval", "--print"})),
    "perl": ("eE", frozenset()),
    "ruby": ("e", frozenset()),
    "php": ("r", frozenset({"--run"})),
    "lua": ("e", frozenset()),
    "luajit": ("e", frozenset()),
    "julia": ("eE", frozenset({"--eval", "--print"})),
    "Rscript": ("e", frozenset()),
    "R": ("ef", frozenset({"--file"})),
    "tsx": ("e", frozenset({"--eval"})),
    "ts-node": ("e", frozenset({"--eval"})),
    "pwsh": ("c", frozenset({"-command"})),
    "powershell": ("c", frozenset({"-command"})),
    "osascript": ("e", frozenset()),
    **dict.fromkeys(
        ("java", "jshell", "kotlin", "scala", "groovy", "swift", "dart", "zig", "escript", "elixir"), _NO_INLINE
    ),
    **dict.fromkeys(("irb", "pry", "tclsh", "expect", "zx", "bb", "clojure", "lein", "racket", "guile"), _NO_INLINE),
}
_STDIN_REDIRECT = re.compile(r"(?<!<)<(?![<(])|<<<")
_AWK_PROGRAM = re.compile(r"system\s*\(|getline|(?<!\|)\|(?!\|)\s*[\"$A-Za-z(]")
_SED_EXEC = re.compile(
    r"(?:^|[;{}\n])\s*(?:\d+|\$|/[^/]*/)?\s*e(?:\s|$)|s(.)(?:(?!\1).)*\1(?:(?!\1).)*\1[gpiImMx0-9]*e"
)
# Read-only and standard tools that take file arguments: a path or script name among their arguments is data.
_SAFE_PATH_TOOLS: Final[frozenset[str]] = frozenset(
    {"ls", "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "ack", "wc", "stat", "file"}
    | {"diff", "cmp", "sort", "uniq", "cut", "tr", "echo", "printf", "pwd", "which", "whereis", "type", "realpath"}
    | {"readlink", "dirname", "basename", "du", "df", "tree", "jq", "yq", "find", "fd", "fdfind", "xxd", "od"}
    | {"hexdump", "strings", "md5sum", "sha1sum", "sha256sum", "shasum", "cksum", "tar", "zip", "unzip", "gzip"}
    | {"gunzip", "zcat", "bzip2", "xz", "cp", "mv", "rm", "mkdir", "rmdir", "touch", "ln", "chmod", "chown"}
    | {"chgrp", "install", "rsync", "tee", "dd", "truncate", "mktemp", "test", "[", "true", "false", "sleep"}
    | {"date", "column", "nl", "fold", "fmt", "rev", "tac", "comm", "join", "paste", "split", "base64", "iconv"}
    | {"patch", "awk", "gawk", "mawk", "nawk", "sed", "gsed", "git", "gh", "docker", "podman", "kubectl", "helm"}
    | {"pytest", "py.test", "ruff", "black", "mypy", "pyright", "flake8", "isort", "pylint", "bandit", "tsc"}
    | {"eslint", "prettier", "vitest", "jest", "mocha", "stylelint", "biome", "rustc", "rustfmt", "gcc", "g++"}
    | {"cc", "c++", "clang", "clang++", "ld", "ar", "nm", "objdump", "javac", "gofmt", "shellcheck", "shfmt"}
    | {"hadolint", "protoc", "openssl", "ssh-keygen", "gpg", "lc", "lemoncrow", "uv", "poetry", "pdm", "pipenv"}
    | {"rye", "cargo", "go", "dotnet", "npm", "pnpm", "yarn", "bun", "deno", "pip", "pip3", "pipx", "conda"}
)
_KNOWN_SAFE: Final[frozenset[str]] = (
    _SAFE_PATH_TOOLS
    | _READERS
    | _FILE_WRITERS
    | _NETWORK_TOOLS
    | _REMOTE_SHELLS
    | _CLOUD_TOOLS
    | _DATABASE_TOOLS
    | frozenset(_INSTALL_VERBS)
    | frozenset(_UPGRADE_VERBS)
)


def _flag_letters(token: str) -> str:
    return token[1:] if token.startswith("-") and not token.startswith("--") else ""


def _pathlike(token: str) -> bool:
    """Whether an argument names a file to run rather than plain data: a path form or a script extension."""

    value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
    if not value or value.startswith("-"):
        return False
    if value.startswith(("./", "../", "/", "~", "$")) or value in {".", ".."}:
        return True
    return posixpath.basename(value.lower()).endswith(_SCRIPT_EXTENSIONS)


def _unlaunch(argv: list[str]) -> tuple[list[str], str | None, str]:
    """What ``uv run`` / ``poetry run`` / ``pdm run`` ... runs: ``(argv, why unresolved, package fetched)``.

    Options before ``run`` are dropped (so ``uv -q pip install x`` is read as ``uv pip install x``) and so are
    ``run``'s own options. An option this table does not know has an unknown arity, so the command is
    unresolved rather than guessed.
    """

    name = posixpath.basename(argv[0])
    if name not in _LAUNCHERS:
        return argv, None, ""
    rest = argv[1:]
    index = 0
    while index < len(rest) and rest[index].startswith("-") and rest[index] != "--":
        flag = rest[index]
        key = flag.split("=", 1)[0]
        if key in _LAUNCHER_VALUE_FLAGS:
            index += 1 if "=" in flag else 2
        elif key in _LAUNCHER_BOOL_FLAGS:
            index += 1
        else:
            return argv, f"is given the option `{_short(flag)}` of `{name}`, whose effect is not known", ""
    if index >= len(rest):
        return argv, None, ""
    if rest[index] != "run":
        return ([argv[0], *rest[index:]] if index else argv), None, ""
    index += 1
    fetched = ""
    while index < len(rest) and rest[index].startswith("-") and len(rest[index]) > 1:
        flag = rest[index]
        key = flag.split("=", 1)[0]
        if flag == "--":
            index += 1
            break
        if key in {"-m", "--module"}:
            module = flag.split("=", 1)[1] if "=" in flag else (rest[index + 1] if index + 1 < len(rest) else "")
            skip = 1 if "=" in flag else 2
            if not module:
                return argv, "runs a module that is not named", fetched
            return ["python", "-m", module, *rest[index + skip :]], None, fetched
        if key in {"-s", "--script"}:
            return argv, "runs a script file, whose commands are not read", fetched
        if key == "--with":
            fetched = flag.split("=", 1)[1] if "=" in flag else (rest[index + 1] if index + 1 < len(rest) else "")
        if key in _LAUNCHER_VALUE_FLAGS:
            index += 1 if "=" in flag else 2
        elif key in _LAUNCHER_BOOL_FLAGS:
            index += 1
        else:
            return argv, f"is given the option `{_short(flag)}` of `{name} run`, whose effect is not known", fetched
    if index < len(rest) and rest[index] == "--":
        index += 1
    inner = rest[index:]
    return (inner or argv), None, fetched


def _interpreter_reason(name: str, rest: list[str], before: str) -> str | None:
    """Why an interpreter's run is not readable: it executes a file, a module or inline program text."""

    inline_letters, inline_long = ("c", frozenset()) if _PYTHON.match(name) else _INLINE_PROGRAM.get(name, _NO_INLINE)
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            index += 1
            break
        if token == "-m" and _PYTHON.match(name):
            module = rest[index + 1] if index + 1 < len(rest) else ""
            if module in _PYTHON_TOOL_MODULES:
                return None
            return f"runs the module `{module or '?'}`, whose commands are not read"
        if token.startswith("--"):
            if token in inline_long or any(token.startswith(flag + "=") for flag in inline_long):
                return "runs inline program text, whose commands are not read"
            index += 1
            continue
        letters = _flag_letters(token)
        if letters:
            if any(letter in inline_letters for letter in letters):
                return "runs inline program text, whose commands are not read"
            index += 1
            continue
        break
    operands = rest[index:]
    if operands:
        return f"runs the script `{_short(operands[0])}`, whose commands are not read"
    if before == "|":
        return "runs a program piped into it, whose commands are not read"
    return None


def _first_verb(rest: list[str], value_flags: frozenset[str]) -> str:
    """The first subcommand-like word of *rest*, skipping flags (and the values of *value_flags*) and ``+toolchain``."""

    index = 0
    while index < len(rest):
        token = rest[index]
        if token in value_flags:
            index += 2
        elif token.startswith(("-", "+")):
            index += 1
        else:
            return token
    return ""


def _git_reason(rest: list[str]) -> str | None:
    """Why a git command line runs something other than git: an alias, a configured program, a hook-like option."""

    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        flag = rest[index]
        if flag in {"-c", "--config-env"}:
            setting = rest[index + 1] if index + 1 < len(rest) else ""
            key, _eq, value = setting.partition("=")
            if key.lower().startswith("alias.") or key.lower() in _GIT_PROGRAM_KEYS or value.startswith("!"):
                return "is given a git setting that runs a program, which is not read"
            if _pathlike(value):
                return "is given a git setting naming a file, which is not read"
        index += 2 if flag in _GIT_VALUE_FLAGS else 1
    verb = rest[index] if index < len(rest) else ""
    tokens = rest[index:]
    if verb and verb not in _GIT_VERBS:
        return f"`git {_short(verb)}` is an alias or an external command, which is not read"
    lowered = [token.lower() for token in tokens]
    if any(token.startswith("alias.") or token.startswith("!") or "=!" in token for token in lowered):
        return "defines or uses a git alias that runs a program, which is not read"
    if verb == "config" and any(token in _GIT_PROGRAM_KEYS for token in lowered):
        return "sets a git option that names a program, which is not read"
    if verb == "bisect" and "run" in tokens:
        return "runs a program at each bisect step, which is not read"
    if verb == "submodule" and "foreach" in tokens:
        return "runs a program in each submodule, which is not read"
    if verb in {"rebase", "clone", "fetch", "pull", "ls-remote", "push", "archive", "grep"}:
        if any(token.startswith(_GIT_PROGRAM_FLAGS) for token in tokens):
            return "is given an option that runs a program, which is not read"
    return None


_CONTAINER_TOOLS: Final[frozenset[str]] = frozenset({"docker", "podman", "nerdctl"})
_PWD_FORMS: Final[tuple[str, ...]] = ("${PWD}", "$PWD", "$(pwd)", "`pwd`")
_VOLUME_FLAG = re.compile(r"^-[A-Za-z]*v$")
_EDITORS_VIM: Final[frozenset[str]] = frozenset(
    {"vim", "vi", "view", "ex", "nvim", "gvim", "gview", "rvim", "rview", "vimdiff", "nvi", "evim", "mvim"}
)
_EDITORS_EMACS: Final[frozenset[str]] = frozenset({"emacs", "emacsclient"})
_EMACS_RUN_FLAGS: Final[frozenset[str]] = frozenset(
    {"-l", "--load", "-load", "-f", "--funcall", "-funcall", "--eval", "-eval", "--execute", "-execute"}
    | {"--script", "-script", "-x", "-e", "--exec"}
)


_CONTAINER_ESCAPE_ANY: Final[frozenset[str]] = frozenset(
    {"--privileged", "--cap-add", "--device", "--security-opt", "--device-cgroup-rule"}
)
_CONTAINER_ESCAPE_HOST: Final[frozenset[str]] = frozenset(
    {"--pid", "--network", "--net", "--ipc", "--uts", "--userns", "--cgroupns"}
)


def _container_escape(token: str, following: str) -> bool:
    """Whether an option gives the container host-level access: privileged, capabilities, devices, host namespaces."""

    key, equals, value = token.partition("=")
    if key in _CONTAINER_ESCAPE_ANY:
        return True
    return key in _CONTAINER_ESCAPE_HOST and (value if equals else following) == "host"


def _container_mounts(rest: list[str]) -> tuple[list[str], bool, bool]:
    """Bind-mount sources of a ``docker|podman run|create`` line, whether one is unreadable, and ``--privileged``.

    Every word is scanned, including those after the image: an option's arity is not parsed, so a flag
    meant for the contained command is read as ours (over-approximate, never under).
    """

    sources: list[str] = []
    unreadable = False
    privileged = False
    specs: list[tuple[str, str]] = []
    for index, token in enumerate(rest):
        following = rest[index + 1] if index + 1 < len(rest) else ""
        if _container_escape(token, following):
            privileged = True
        elif token in {"--volume", "--mount"} or _VOLUME_FLAG.match(token):
            specs.append(("mount" if token == "--mount" else "volume", following))
        elif token.startswith(("--volume=", "--mount=", "-v=")):
            key, _eq, value = token.partition("=")
            specs.append(("mount" if key == "--mount" else "volume", value))
        elif token.startswith("-") and not token.startswith("--"):
            # An option cluster whose flag letter takes the rest of the token: ``-itv/etc:/h``.
            letters = re.match(r"-[A-Za-z]*", token)
            run = letters.group(0) if letters else ""
            specs.extend(("volume", token[at + 1 :]) for at, char in enumerate(run) if char == "v" and at > 0)
    for kind, value in specs:
        if kind == "volume":
            if ":" not in value:
                continue  # an anonymous volume
            source = value.split(":", 1)[0]
        else:
            fields = dict(part.partition("=")[::2] for part in value.split(",") if part)
            if '"' in value or "'" in value:
                unreadable = True
                continue
            kind_of = fields.get("type", "")
            source = fields.get("source", fields.get("src", ""))
            if kind_of and kind_of != "bind" and not source.startswith(("/", ".", "~", "$")):
                continue  # a named volume or tmpfs
            if kind_of == "bind" and not source:
                unreadable = True
                continue
            if not source:
                continue
        for form in _PWD_FORMS:
            if source.startswith(form):
                source = "." + source[len(form) :]
        if not source or "$" in source.replace("$HOME", "", 1) or "`" in source or "(" in source:
            unreadable = True
        elif source.startswith(("/", ".", "~", "$HOME")) or "/" in source:
            sources.append(source)
    return sources, unreadable, privileged


def _is_container_run(tool: str, rest: list[str]) -> bool:
    return tool in _CONTAINER_TOOLS and bool({"run", "create"} & set(rest[:3]))


def _editor_reason(name: str, rest: list[str]) -> str | None:
    """Why an editor invocation runs commands: ex commands, script files or Lisp given on its command line."""

    if name in _EDITORS_VIM:
        for index, token in enumerate(rest):
            letters = _flag_letters(token)
            if token in {"-c", "--cmd", "-S", "-s", "-es", "-Es", "--startuptime"} or token.startswith("--cmd"):
                return "is given editor commands or a script, which are not read"
            if (letters and any(letter in "cSs" for letter in letters)) or (token.startswith("-c") and len(token) > 2):
                return "is given editor commands or a script, which are not read"
            if token == "-u" and (rest[index + 1 : index + 2] or [""])[0] not in {"NONE", "NORC"}:
                return "loads an editor startup script, which is not read"
            if token.startswith("+") and len(token) > 1 and not token[1:].isdigit():
                return "is given an editor command, which is not read"
    if name in _EDITORS_EMACS:
        for token in rest:
            if token.split("=", 1)[0] in _EMACS_RUN_FLAGS:
                return "runs Lisp or a Lisp file, which is not read"
    return None


def _unreadable_program(argv: list[str], before: str, raw: str) -> str | None:
    """Why what *argv* runs cannot be read from its text, or ``None`` (a plain program with plain arguments).

    Covers a file run by path or sourced, a shell run on a script file, task and
    package-script runners, wrappers, interpreters given a file, module or inline
    text, and, for any program not on the known-safe list, a path or script name
    among its arguments (fail closed).
    """

    head = argv[0]
    name = posixpath.basename(head)
    rest = argv[1:]
    if "/" in head and not head.startswith(_SYSTEM_BIN_DIRS):
        return f"runs the file `{_short(head)}`, whose commands are not read"
    if name in {"source", "."} and rest:
        return f"sources the file `{_short(rest[0])}`, whose commands are not read"
    if name.lower().endswith(_SCRIPT_EXTENSIONS) and not name.startswith("-"):
        return f"runs the file `{_short(head)}`, whose commands are not read"
    if name in _TASK_RUNNERS:
        return "runs the recipes of a file in the repository, which are not read"
    if name in _UNPARSED_WRAPPERS and rest:
        return "runs another command whose options are not parsed, so it is not read"
    if name in _RUNNER_VERBS and any(token in _RUNNER_VERBS[name] for token in rest):
        return "runs a program that is not read"
    if name in {"npm", "pnpm", "yarn", "bun"}:
        verbs = _PACKAGE_SCRIPT_VERBS if name != "yarn" else _PACKAGE_SCRIPT_VERBS - {"init", "create"}
        if any(token in verbs for token in rest):
            return "runs scripts of the repository's package.json, which are not read"
        first = next((token for token in rest if not token.startswith("-")), "")
        builtins = {"npm": _NPM_BUILTINS, "pnpm": _PNPM_BUILTINS, "yarn": _YARN_BUILTINS, "bun": _BUN_BUILTINS}[name]
        if first and first not in builtins:
            return "runs a script or file, which is not read"
        return None
    if name in _RUNNERS:
        if any(token in {"-c", "--call"} for token in rest):
            return "runs command text, which is not read"
        inner = rest
        while inner and inner[0].startswith("-"):
            inner = inner[2:] if inner[0] in {"-p", "--package"} else inner[1:]
        return _unreadable_program(inner, before, raw) if inner else None
    if name == "git":
        return _git_reason(rest)
    if _is_container_run(name, rest):
        _sources, unreadable, privileged = _container_mounts(rest)
        if privileged:
            return "runs a container with host-level access (privileged, capabilities, devices, host namespaces), which is not read"
        if unreadable:
            return "mounts a host path that is only known when it runs"
    editor = _editor_reason(name, rest)
    if editor:
        return editor
    if name == "cargo":
        verb = _first_verb(rest, _CARGO_VALUE_FLAGS)
        return None if not verb or verb in _CARGO_SAFE else "builds and runs repository code, which is not read"
    if name == "go":
        verb = _first_verb(rest, frozenset({"-C"}))
        return None if not verb or verb in _GO_SAFE else "builds and runs repository code, which is not read"
    if name == "dotnet":
        verb = _first_verb(rest, frozenset())
        return None if not verb or verb in _DOTNET_SAFE else "builds and runs repository code, which is not read"
    if name == "deno":
        first = next((token for token in rest if not token.startswith("-")), "")
        return "runs code that is not read" if first and first not in _DENO_BUILTINS else None
    if _PYTHON.match(name) or name in _INLINE_PROGRAM:
        return _interpreter_reason(name, rest, before)
    if name in {"awk", "gawk", "mawk", "nawk"} and any(
        token in {"-f", "--file"} or token.startswith("--file=") or _AWK_PROGRAM.search(token) for token in rest
    ):
        return "runs an awk program that reads a file or runs commands, which is not read"
    if name in {"sed", "gsed"} and any(
        token in {"-f", "--file"} or token.startswith("--file=") or _SED_EXEC.search(token) for token in rest
    ):
        return "runs a sed script that reads a file or runs commands, which is not read"
    if name == "find" and any(token in {"-exec", "-execdir", "-ok", "-okdir"} for token in rest):
        return "runs another command per match, which is not read"
    if name == "parallel":
        return "runs commands built from its input, which are not read"
    if name == "su" and not any(token == "-c" or token.startswith("--command") for token in rest):
        return "starts a shell as another user, whose commands are not read"
    if name == "su" and any(token == "--command" or token == "-c" or token.startswith("--command=") for token in rest):
        return "runs command text as another user, which is not read"
    if name not in _KNOWN_SAFE and any(_pathlike(token) for token in rest):
        return "is given a file or path argument and is not a known read-only tool, so what it runs is not read"
    return None


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _closing(text: str, start: int, opener: str) -> tuple[int, bool]:
    """Where the substitution whose body starts at *start* ends, and whether it is closed.

    *opener* is ``"`"`` for a backtick body, else ``"("`` (``$(``, ``<(``, ``>(``).
    """

    depth = 1
    quote = ""
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if opener == "`":
            if char == "`":
                return index + 1, True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1, True
        index += 1
    return len(text), False


def _scan(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Top-level shell segments (each with the operator before it) and every substitution body.

    Quote-aware: operators inside quotes or substitutions never split. ``$(...)``,
    ``<(...)``, ``>(...)`` and backtick bodies run as commands (also inside double
    quotes), so they are returned to be classified too. Unbalanced text is read
    best-effort: an unclosed quote or substitution runs to the end.
    """

    segments: list[tuple[str, str]] = []
    bodies: list[str] = []
    current: list[str] = []
    before = ""
    quote = ""
    index = 0

    def cut(operator: str) -> None:
        nonlocal before
        segment = "".join(current).strip()
        if segment:
            segments.append((before, segment))
        current.clear()
        before = operator

    while index < len(text):
        char = text[index]
        if quote == "'":
            current.append(char)
            quote = "" if char == "'" else quote
            index += 1
            continue
        if char == "\\":
            current.append(text[index : index + 2])
            index += 2
            continue
        if char == "`" or (text.startswith("(", index + 1) and (char == "$" or (char in "<>" and not quote))):
            start = index + (1 if char == "`" else 2)
            end, closed = _closing(text, start, "`" if char == "`" else "(")
            bodies.append(text[start : end - 1 if closed else end])
            current.append(text[index:end])
            index = end
            continue
        if quote == '"' or char in "'\"":
            current.append(char)
            quote = ("" if char == '"' else quote) if quote else char
            index += 1
            continue
        if char in "|&" and text.startswith(char, index + 1):
            cut(char * 2)
            index += 2
        elif char == "|":
            cut("|")
            index += 2 if text.startswith("&", index + 1) else 1
        elif char == "&" and not (index and text[index - 1] in "<>") and not text.startswith(">", index + 1):
            cut("&")
            index += 1
        elif char in ";\n()":
            cut(char)
            index += 1
        else:
            current.append(char)
            index += 1
    cut("")
    return segments, bodies


def _strip_wrapper(name: str, rest: list[str]) -> list[str]:
    """What one wrapper command runs: its words past its own options (and timeout's duration)."""

    flags = _WRAPPER_VALUE_FLAGS.get(name, frozenset())
    index = 0
    duration = name == "timeout"
    while index < len(rest):
        token = rest[index]
        if token == "--":
            index += 1
            break
        if name == "env" and token in _ENV_SPLIT_FLAGS:
            # ``env -S`` splits its value into the command line it runs.
            return _tokens(rest[index + 1]) + rest[index + 2 :] if index + 1 < len(rest) else []
        if token in flags:
            index += 2
        elif (
            (token.startswith("-") and len(token) > 1)
            or token.isdigit()
            or (name == "env" and _ASSIGNMENT.match(token))
        ):
            index += 1
        elif duration and _TIMEOUT_DURATION.match(token):
            duration = False
            index += 1
        else:
            break
    return rest[index:]


def _escalated_shell(name: str, rest: list[str]) -> bool:
    """Whether sudo/doas options ask for a shell as the other user (``-s``, ``-i``, ``--shell``, ``--login``)."""

    flags = _WRAPPER_VALUE_FLAGS[name]
    index = 0
    while index < len(rest) and rest[index].startswith("-") and rest[index] != "--":
        token = rest[index]
        if token in flags:
            index += 2
            continue
        if token in {"--shell", "--login"}:
            return True
        if not token.startswith("--") and any(letter in "si" for letter in token[1:]):
            return True
        index += 1
    return False


def _xargs_replace_string(rest: list[str]) -> str:
    """The text xargs replaces with each input item (``-I{}``, ``-i``, ``--replace``), or ``""``."""

    for index, token in enumerate(rest):
        if token == "--" or not token.startswith("-"):
            if index == 0 or rest[index - 1] not in _WRAPPER_VALUE_FLAGS["xargs"]:
                break
            continue
        if token in {"-I", "-J"} and index + 1 < len(rest):
            return rest[index + 1]
        if token.startswith(("-I", "-J")) and len(token) > 2:
            return token[2:]
        if token == "--replace" or token == "-i":
            return "{}"
        if token.startswith("--replace="):
            return token.split("=", 1)[1] or "{}"
        if token.startswith("-i") and len(token) > 2 and not token.startswith("--"):
            return token[2:]
    return ""


def _unwrap(argv: list[str]) -> tuple[list[str], bool]:
    """The command a segment really runs, and whether xargs appends arguments to it.

    Drops env assignments, shell keywords, function heads and wrapper commands
    with their options. A wrapper with nothing left to run is itself the command
    (``env`` alone prints the environment).
    """

    fed = False
    while argv:
        head = argv[0]
        if _ASSIGNMENT.match(head) or head in _SHELL_KEYWORDS or _FUNCTION_HEAD.match(head):
            argv = argv[1:]
            continue
        if len(argv) > 1 and (head.startswith("-") or head.isdigit()):
            argv = argv[1:]
            continue
        name = posixpath.basename(head)
        if name not in _WRAPPERS:
            break
        inner = _strip_wrapper(name, argv[1:])
        if name in {"sudo", "doas"} and _escalated_shell(name, argv[1:]):
            # ``sudo -s`` / ``-i`` runs its operands as shell text, and with none opens a shell.
            inner = ["sh", "-c", " ".join(inner)] if inner else ["su"]
        if not inner:
            break
        if name == "xargs":
            fed = True
            if (marker := _xargs_replace_string(argv[1:])) and marker in inner[0]:
                inner = ["${XARGS_INPUT}", *inner[1:]]  # the command word itself comes from the input
        argv = inner
    return argv, fed


def _shell_script(argv: list[str]) -> tuple[str | None, bool]:
    """A shell's ``-c`` command string (``None`` without ``-c``) and whether it reads commands from stdin."""

    command_string = False
    from_stdin = False
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            index += 1
            break
        if token in _SHELL_VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith("--"):
            index += 1
            continue
        if len(token) > 1 and token[0] in "-+":
            if token[0] == "-":
                command_string = command_string or "c" in token[1:]
                from_stdin = from_stdin or "s" in token[1:]
            index += 2 if token[-1] in "oO" else 1
            continue
        break
    operands = argv[index:]
    if command_string:
        return (operands[0] if operands else ""), False
    return None, from_stdin or not operands or operands[0] == "-"


def split_commands(command: str) -> list[list[str]]:
    """Top-level shell segments as argument vectors, with env assignments and wrappers dropped."""

    out: list[list[str]] = []
    for _before, raw in _scan(command)[0]:
        argv, _fed = _unwrap(_tokens(raw))
        if argv:
            argv[0] = posixpath.basename(argv[0])
            out.append(argv)
    return out


def _home_or_absolute(path: str) -> bool:
    return path.startswith(("/", "~", "$HOME", "${HOME}"))


def outside_workspace(path: str, repo_root: str = "") -> bool:
    """Whether writing *path* leaves the workspace (temp dirs and devices never do)."""

    text = path.strip().strip("'\"")
    if not text or text in _DEVICE_SINKS or text.startswith(("/proc/self/fd/", "/dev/fd/")):
        return False
    if text.startswith(("$TMPDIR", "${TMPDIR", "~/.cache/", "$HOME/.cache/")):
        return False
    if not _home_or_absolute(text):
        normalized = posixpath.normpath(text)
        return normalized == ".." or normalized.startswith("../")
    if text.startswith("/"):
        normalized = posixpath.normpath(text)
        if any((normalized + "/").startswith(root) for root in _TEMP_ROOTS):
            return False
        root = posixpath.normpath(repo_root) if repo_root else ""
        if root and (normalized == root or normalized.startswith(root + "/")):
            return False
    return True


def _with_url(argv: list[str]) -> list[str]:
    candidates = [arg for arg in argv[1:] if not arg.startswith("-") and _URLISH.match(arg)]
    schemed = [arg for arg in candidates if "://" in arg]
    return schemed or candidates


def _classify_segment(argv: list[str], repo_root: str, redirects: list[str]) -> LedgerClass | None:
    tool = argv[0]
    rest = argv[1:]
    joined = " ".join(argv)
    if _SECRET_TOOLS.match(joined):
        return LedgerClass("secret_access", f"reads a secret store ({tool})")
    if tool in _READERS and any(_SECRET_PATHS.search(arg) for arg in rest if not arg.startswith("-")):
        return LedgerClass("secret_access", "reads a credential file")
    if tool in {"printenv", "env", "set"} and not rest:
        return LedgerClass("secret_access", "dumps the environment")
    if tool in {"echo", "printf"} and _SECRET_ENV.search(joined):
        return LedgerClass("secret_access", "prints a secret environment variable")
    if tool == "git" and "push" in rest:
        forced = any(arg in {"-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d"} for arg in rest)
        return LedgerClass("git_push", "force push" if forced else "pushes to a remote")
    if tool == "gh" and rest[:2] in (
        ["pr", "merge"],
        ["repo", "delete"],
        ["release", "create"],
        ["release", "delete"],
    ):
        return LedgerClass("git_push", f"gh {' '.join(rest[:2])}")
    if tool in _CLOUD_TOOLS:
        if tool == "terraform" and rest[:1] in (["fmt"], ["validate"], ["version"], ["-version"]):
            return None
        return LedgerClass("cloud", f"{tool} {' '.join(arg for arg in rest[:2] if not arg.startswith('-'))}".strip())
    if tool == "docker" and rest[:1] in (["push"], ["login"]):
        return LedgerClass("cloud", f"docker {rest[0]}")
    if _is_container_run(tool, rest):
        for source in _container_mounts(rest)[0]:
            if outside_workspace(source, repo_root):
                return LedgerClass("outside_write", f"mounts {source} into a container")
    if tool in _DATABASE_TOOLS or _MIGRATION_COMMANDS.search(joined):
        return LedgerClass("database", f"runs {tool}" if tool in _DATABASE_TOOLS else "runs database migrations")
    for target in redirects:
        if outside_workspace(target, repo_root):
            return LedgerClass("outside_write", f"writes {target}")
    if tool in _FILE_WRITERS:
        operands = [arg for arg in rest if not arg.startswith("-")]
        targets = operands if tool in {"rm", "mkdir", "touch", "chmod", "chown", "tee"} else operands[-1:]
        if tool == "dd":
            targets = [arg.split("=", 1)[1] for arg in rest if arg.startswith("of=")]
        for target in targets:
            if tool == "rsync" and ":" in target and not target.startswith("/"):
                return LedgerClass("network", "rsync to a remote host")
            if outside_workspace(target, repo_root):
                return LedgerClass("outside_write", f"{tool} {target}")
    upgrade = _UPGRADE_VERBS.get(tool)
    if upgrade is not None and rest and rest[0] in upgrade:
        # A dependency upgrade rewrites the lockfile from the registry (P12-AGENT).
        named = ", ".join(arg for arg in rest[1:5] if not arg.startswith("-")) or "dependencies"
        return LedgerClass("package_install", f"{tool} {rest[0]} {named}")
    if (
        tool in {"uv", "poetry", "pdm"}
        and rest[:1] in (["lock"], ["sync"])
        and ({"--upgrade", "-U", "--update"} & set(rest) or any(arg.startswith("--upgrade-package") for arg in rest))
    ):
        return LedgerClass("package_install", f"{tool} {rest[0]} --upgrade")
    verbs = _INSTALL_VERBS.get(tool)
    if _PYTHON.match(tool) and rest[:3] == ["-m", "pip", "install"]:
        verbs, rest, tool = _INSTALL_VERBS["pip"], rest[2:], "pip"
    if tool == "uv" and rest[:2] == ["pip", "install"]:
        verbs, rest, tool = _INSTALL_VERBS["pip"], rest[1:], "pip"
    if tool == "uv" and rest[:2] in (["tool", "install"], ["pip", "sync"]):
        return LedgerClass("package_install", "installs from the registry or a manifest")
    if tool == "go" and rest[:2] in (["mod", "download"], ["mod", "tidy"], ["mod", "vendor"]):
        return LedgerClass("package_install", f"go mod {rest[1]} fetches modules")
    if (
        tool in {"yarn", "bundle"}
        and not any(not arg.startswith("-") for arg in rest)
        and not ({"--version", "-v", "--help", "-h"} & set(rest))
    ):
        return LedgerClass("package_install", f"{tool} installs the manifest's dependencies")
    if verbs is not None and rest and rest[0] in verbs:
        # A manifest install (``-r requirements.txt``, ``npm ci``, ``poetry install``) counts too: the
        # agent can edit the manifest and then install it.
        packages = [arg for arg in rest[1:] if not arg.startswith("-")]
        flags = [arg for arg in rest[1:] if arg.startswith("-")]
        scope = " globally" if {"-g", "--global"} & set(flags) or tool in {"brew", "apt", "apt-get"} else ""
        named = ", ".join(packages[:4]) or "the manifest's packages"
        return LedgerClass("package_install", f"{tool} {rest[0]} {named}{scope}")
    if tool == "uvx" and rest:
        return LedgerClass("package_install", f"runs {rest[0]} fetched by uvx")
    if tool in _RUNNERS and rest:
        specs = [arg for arg in rest if not arg.startswith("-")]
        if _FETCH_FLAGS & set(rest) or (specs and "@" in specs[0][1:]):
            return LedgerClass("package_install", f"runs {specs[0] if specs else 'a package'} fetched by {tool}")
    if tool in _NETWORK_TOOLS:
        urls = _with_url(argv)
        if urls and all(_LOOPBACK.match(url) for url in urls):
            return None
        target = urls[0] if urls else ""
        return LedgerClass("network", f"{tool} {target}".strip())
    if tool in _REMOTE_SHELLS:
        hosts = [arg for arg in rest if not arg.startswith("-")]
        if hosts and _LOOPBACK.match(hosts[0].split(":", 1)[0]):
            return None
        return LedgerClass("network", f"{tool} to a remote host")
    if tool == "gh" and rest[:1] in (["api"], ["pr"], ["issue"], ["release"], ["workflow"], ["run"], ["secret"]):
        if rest[:1] == ["secret"]:
            return LedgerClass("secret_access", "gh secret")
        return LedgerClass("network", f"GitHub API: gh {' '.join(rest[:2])}")
    return None


_REDIRECT = re.compile(r"(?<![<0-9&])(?:[0-9]{0,2}>>?|&>>?)\s*([^\s;|&<>]+)")
_PIPE_TO_SHELL = re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")


@dataclass(frozen=True, slots=True)
class CommandAnalysis:
    """What a shell command's text shows it does.

    ``unresolved`` names each part whose effect cannot be read from the text:
    a command word computed at run time, commands piped into a shell, or command
    text nested deeper than :data:`MAX_SHELL_DEPTH`. Approval treats those as
    every configured class (fail closed). That includes a command that runs a
    script file, task runner or other interpreter (see :func:`_unreadable_program`).
    """

    classes: tuple[LedgerClass, ...]
    unresolved: tuple[str, ...]


def _short(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= 80 else flat[:77] + "..."


def _analyze(text: str, repo_root: str, depth: int, found: list[LedgerClass], unresolved: list[str]) -> None:
    if not text:
        return
    if depth > MAX_SHELL_DEPTH:
        unresolved.append(f"`{_short(text)}` nests command text more than {MAX_SHELL_DEPTH} levels deep")
        return
    pipe_to_shell = bool(_PIPE_TO_SHELL.search(text))
    segments, bodies = _scan(text)
    for before, raw in segments:
        redirects = [match.group(1) for match in _REDIRECT.finditer(raw)]
        argv, fed = _unwrap(_tokens(_REDIRECT.sub(" ", raw).strip()))
        for _round in range(MAX_LAUNCH_ROUNDS):
            if not argv:
                break
            launched, why, fetched = _unlaunch(argv)
            if fetched:
                found.append(LedgerClass("package_install", f"{posixpath.basename(argv[0])} run --with {fetched}"))
            if why:
                unresolved.append(f"`{_short(raw)}` {why}")
                break
            if launched == argv:
                break
            argv, more = _unwrap(launched)
            fed = fed or more
        else:
            unresolved.append(f"`{_short(raw)}` nests wrappers and launchers more than {MAX_LAUNCH_ROUNDS} levels deep")
        if argv and ("$" in argv[0] or "`" in argv[0]):
            unresolved.append(f"the command word of `{_short(raw)}` is computed only when it runs")
            continue
        name = posixpath.basename(argv[0]) if argv else ""
        if name == "eval":
            _analyze(" ".join(argv[1:]).strip(), repo_root, depth + 1, found, unresolved)
            argv = []
        elif name in _SHELLS:
            script, from_stdin = _shell_script(argv)
            if script is not None:
                if fed:
                    # xargs substitutes or appends its input into the shell's command text.
                    unresolved.append(f"`{_short(raw)}` runs command text that xargs completes from its input")
                _analyze(script.strip(), repo_root, depth + 1, found, unresolved)
            elif from_stdin and before == "|":
                unresolved.append(f"`{_short(raw)}` runs commands piped into a shell")
            elif from_stdin and _STDIN_REDIRECT.search(raw):
                unresolved.append(f"`{_short(raw)}` runs commands redirected into a shell")
            elif not from_stdin:
                unresolved.append(f"`{_short(raw)}` runs a script file, whose commands are not read")
            argv = []
        reason = _unreadable_program(argv, before, raw) if argv else None
        if reason:
            unresolved.append(f"`{_short(raw)}` {reason}")
        if argv:
            argv = [name, *argv[1:], *([XARGS_INPUT] if fed else [])]
        elif redirects:
            argv = ["true"]  # a bare redirect, or the redirects of a shell or eval
        else:
            continue
        # Classify the command itself, then add a write outside the workspace: one class per
        # segment would otherwise hide either behind the other.
        current = _classify_segment(argv, repo_root, [])
        if current is not None:
            if pipe_to_shell and current.category == "network":
                current = LedgerClass("package_install", f"downloads and runs a script ({current.label})")
            found.append(current)
        if current is None or current.category != "outside_write":
            hidden = next((target for target in redirects if outside_workspace(target, repo_root)), None)
            if hidden is not None:
                found.append(LedgerClass("outside_write", f"writes {hidden}"))
    for body in bodies:
        _analyze(body.strip(), repo_root, depth + 1, found, unresolved)


def analyze_command(command: str, *, repo_root: str = "") -> CommandAnalysis:
    """Every consequential class of a shell command, plus what cannot be read from its text.

    Quote-aware segments; wrappers (``sudo``, ``env``, ``timeout``, ``xargs`` ...)
    are unwrapped, and command text run by ``sh -c`` / ``bash -lc`` / ``eval`` or
    inside ``$(...)`` and backticks is classified recursively, at most
    :data:`MAX_SHELL_DEPTH` levels deep.

    Commands whose effect lives in a file or another interpreter (``bash script.sh``,
    ``source f``, ``./script``, ``make``, ``npm run x``, ``python file.py``,
    ``python -c``, ``node file.js``, ``node -e``, ``find -exec``, ``parallel``,
    ``su -c`` ...) are *unresolved*: their text is never read or executed, because
    the only copy at gate time is the agent's own mutable worktree.

    lc-debt: functions and aliases defined earlier in a shell session, and tools
    that run commands from their own configuration (``pytest`` conftest, ``tox``,
    ``gradle``, ``cargo`` build scripts, ``python -m`` of a listed tool module)
    are classified as the tool, not as what it runs. Upgrade: classify at the
    host's process level.
    """

    found: list[LedgerClass] = []
    unresolved: list[str] = []
    _analyze(command.strip(), repo_root, 0, found, unresolved)
    return CommandAnalysis(tuple(found), tuple(dict.fromkeys(unresolved)))


def classify_command_all(command: str, *, repo_root: str = "") -> list[LedgerClass]:
    """Every consequential class of a shell command, one per classified segment, in order.

    Approval gating (P12-AGENT) needs each class a compound command touches, not
    only the most severe: ``git push && npm i left-pad`` is both a push and an
    install, and either may be the one a team configured.
    """

    return list(analyze_command(command, repo_root=repo_root).classes)


def classify_command(command: str, *, repo_root: str = "") -> LedgerClass | None:
    """The most severe consequential class of a shell command, or ``None``."""

    found: LedgerClass | None = None
    for current in classify_command_all(command, repo_root=repo_root):
        if found is None or _RANK[current.category] < _RANK[found.category]:
            found = current
    return found


def classify_outside_write(path: str, repo_root: str) -> LedgerClass | None:
    """An agent's file write/edit outside the repository and temp directories."""

    text = path.strip()
    if not text.startswith("/") or not outside_workspace(text, repo_root):
        return None
    if _SECRET_PATHS.search(text):
        return LedgerClass("secret_access", f"edits credential file {text}")
    return LedgerClass("outside_write", f"edits {text}")


__all__ = [
    "LEDGER_CATEGORIES",
    "LEDGER_LABELS",
    "MAX_SHELL_DEPTH",
    "XARGS_INPUT",
    "CommandAnalysis",
    "LedgerCategory",
    "LedgerClass",
    "analyze_command",
    "classify_command",
    "classify_command_all",
    "classify_outside_write",
    "outside_workspace",
    "split_commands",
]
