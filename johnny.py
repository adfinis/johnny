# SPDX-FileCopyrightText: 2020 Adfinis-SyGroup
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import sys
from itertools import groupby
from subprocess import check_output

import click
import requests
import toml
from packaging import version

github_base = "https://api.github.com/repos"
gitlab_base = "https://gitlab.com"
arch_base = "https://www.archlinux.org/packages/search/json"
aur_base = "https://aur.archlinux.org/rpc"

session = requests.Session()


def git(args, pkgs):
    res = {}
    for name, pkg in pkgs.items():
        primary = pkg.get("primary")
        base = pkg.get("url")
        if base and primary == "git":
            out = check_output(["git", "ls-remote", "--tags", base]).decode("UTF-8")
            vers = set()
            for line in out.splitlines():
                _, _, tag = line.partition("\t")
                tag = tag.split("/")[-1]
                tag, _, _ = tag.partition("^")
                vers.add(tag)
            vers = try_parse_versions(vers)
            if vers:
                res[name] = vers[-1]
    return res


def gitlab(args, pkgs, type="releases", field="tag_name"):
    res = {}
    arg_gitlab_token = args["gitlab_token"]
    for name, pkg in pkgs.items():
        id_ = pkg.get("gitlab")
        if id_:
            id_ = id_.replace("/", "%2F")
            base = pkg.get("url", gitlab_base)
            headers = {}
            if arg_gitlab_token and base == github_base:
                headers = {"Private-Token": f"token {arg_gitlab_token}"}
            r = session.get(
                f"{base}/api/v4/projects/{id_}/{type}", headers=headers
            ).json()
            if r:
                vers = [x[field] for x in r if field in x]
                vers = try_parse_versions(vers)
                if vers:
                    res[name] = vers[-1]
    return res


def gitlab_tags(args, pkgs):
    return gitlab(args, pkgs, "repository/tags", "name")


def github(args, pkgs, type="releases", field="tag_name"):
    res = {}
    arg_github_token = args["github_token"]
    for name, pkg in pkgs.items():
        id_ = pkg.get("github")
        if id_:
            headers = {}
            if arg_github_token:
                headers = {"Authorization": f"token {arg_github_token}"}
            r = session.get(f"{github_base}/{id_}/{type}", headers=headers).json()
            if r:
                vers = [x[field] for x in r if field in x]
                vers = try_parse_versions(vers)
                if vers:
                    res[name] = vers[-1]
    return res


def github_tags(args, pkgs):
    return github(args, pkgs, "tags", "name")


def arch(args, pkgs):
    res = {}
    for name, pkg in pkgs.items():
        id_ = pkg.get("arch", name)
        r = session.get(f"{arch_base}/?name={id_}").json()
        r = r["results"]
        if r:
            vers = try_parse_versions([r[0]["pkgver"]])
            if vers:
                res[name] = vers[0]
    return res


def aur(args, pkgs):
    query = []
    items = list(pkgs.items())
    for name, pkg in items:
        id_ = pkg.get("aur", name)
        query.append(f"arg[]={id_}")
    query = "&".join(query)
    r = session.get(f"{aur_base}/?v=5&type=info&{query}").json()
    r = r["results"]
    res = {}
    for i, v in enumerate(r):
        if v:
            vers = try_parse_versions([v["Version"]])
            if vers:
                res[items[i][0]] = vers[0]
    return res


sources_list = [github, gitlab, aur, arch]
sources = {x.__name__: x for x in sources_list}
sources["github_tags"] = github_tags
sources["gitlab_tags"] = gitlab_tags
sources["git"] = git


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def try_parse_versions(versions):
    res = []
    for ver in versions:
        ver = ver.strip("v")
        ver = version.parse(ver)
        if not isinstance(ver, version.LegacyVersion):
            ver = version.parse(ver.base_version)
            res.append(ver)
    return sorted(res)


def update(old, new):
    res = dict(old)
    for k, v in new.items():
        if k not in res:
            res[k] = v
        else:
            ov = res[k]
            if v > ov:
                res[k] = v
    return res


def make_serializable(s):
    return {k: str(v) for k, v in s.items()}


def status(args, source, query, new, all):
    if args["quiet"]:
        return
    if args["print_names"]:
        squery = ", ".join(query)
        eprint(f"Asking {source} for:\n    {squery}")
        if new:
            snew = ", ".join(new.keys())
            eprint(f"found:\n    {snew}, total: {all}\n")
        else:
            eprint(f"total: {all}\n")
    else:
        eprint(
            f"Asking {source} for {len(query)} packages, found {len(new)}, total: {all}"
        )


def get_primary(args, c, vers):
    primary = [(k, v) for k, v in c.items() if "primary" in v]
    primary = sorted(primary, key=lambda i: i[1]["primary"])
    primary = groupby(primary, key=lambda i: i[1]["primary"])
    vers = dict(vers)
    asked = set()
    for k, g in primary:
        g = list(g)
        x = dict(g)
        asked.add(k)
        s = sources[k]
        new = s(args, x)
        vers = update(vers, new)
        status(args, k, x, new, len(vers))
    return vers, asked


def get_secondary_source(args, c, s, vers, left):
    new = s(args, left)
    vers = update(vers, new)
    status(args, s.__name__, left, new, len(vers))
    arg_trust_secondary = args["trust_secondary"]
    if arg_trust_secondary:
        return vers, {k: v for k, v in c.items() if k not in vers}
    return vers, left


def run_secondary(args, c, vers, asked, left, l):
    if left:
        for s in [x for x in sources_list if l(x.__name__, asked)]:
            vers, left = get_secondary_source(args, c, s, vers, left)
            if not left:
                break
    return vers, left


def get_secondary(args, c, vers, asked, left):
    vers = dict(vers)
    # Do not ask the sources we just asked (a slight optimization)
    vers, left = run_secondary(
        args, c, vers, asked, left, lambda name, asked: name not in asked
    )
    vers, left = run_secondary(
        args, c, vers, asked, left, lambda name, asked: name in asked
    )
    return vers, left


def get_vers(args, c):
    arg_primary = args["primary"]
    arg_secondary = args["secondary"]
    arg_trust_primary = args["trust_primary"]
    vers = {}
    asked = set()
    if arg_primary:
        vers, asked = get_primary(args, c, vers)
    if arg_trust_primary:
        left = {k: v for k, v in c.items() if k not in vers}
    else:
        left = dict(c)
    if arg_secondary and left:
        vers, left = get_secondary(args, c, vers, asked, left)
    left = ", ".join([k for k in left.keys()])
    if left:
        eprint(f"Packages left: {left}")
    return vers, left


defaults = {
    "primary": True,
    "secondary": True,
    "trust_primary": True,
    "trust_secondary": True,
    "print_names": False,
    "quiet": False,
}


def read_config(args, config):
    args = dict(args)
    for k in config.keys():
        if k not in args:
            raise KeyError(k, "Unknown config option")
    for k, v in args.items():
        if v is None:
            args[k] = config.get(k, defaults.get(k))
    return args


@click.command(
    help=(
        "johnny - generic dep(p)endencies tracker\n\n"
        "command-line options take precedence over config options.\n\n"
        "tokens are only needed for high rate queries. (rate-limit)"
    )
)
@click.argument("config", type=click.File("r", encoding="UTF-8"))
@click.option("--github-token", type=click.STRING, help="github token")
@click.option("--gitlab-token", type=click.STRING, help="gitlab token")
@click.option("--primary/--no-primary", default=None, help="query primary sources")
@click.option("--secondary/--no-secondary", default=None, help="query primary sources")
@click.option(
    "--trust-primary/--no-trust-primary", default=None, help="trust primary sources"
)
@click.option(
    "--trust-secondary/--no-trust-secondary",
    default=None,
    help="trust secondary sources",
)
@click.option(
    "--print-names/--no-print-names",
    default=None,
    help="print package names instead of count",
)
@click.option(
    "--quiet/--no-quiet", default=None, help="do not print anything to stderr",
)
def cli(config, **kwargs):
    c = toml.load(config)
    jc = c.get("johnny_config", {})
    kwargs = read_config(kwargs, jc)
    if jc:
        del c["johnny_config"]
    vers, left = get_vers(kwargs, c)
    print(json.dumps(make_serializable((vers))))
    if left:
        sys.exit(1)


if __name__ == "__main__":
    cli()
