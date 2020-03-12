# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import sys
from itertools import groupby

import click
import requests
import toml
from packaging import version

github_base = "https://api.github.com/repos"
gitlab_base = "https://gitlab.com"
arch_base = "https://www.archlinux.org/packages/search/json"
aur_base = "https://aur.archlinux.org/rpc"


def gitlab(args, pkgs, type="releases", field="tag_name"):
    res = {}
    for name, pkg in pkgs.items():
        id_ = pkg.get("gitlab")
        if id_:
            id_ = id_.replace("/", "%2F")
            base = pkg.get("url", gitlab_base)
            r = requests.get(f"{base}/api/v4/projects/{id_}/{type}")
            j = json.loads(r.text)
            if j:
                vers = [x[field] for x in j if field in x]
                vers = try_parse_versions(vers)
                res[name] = vers[-1]
    return res


def gitlab_tags(args, pkgs):
    return gitlab(args, pkgs, "repository/tags", "name")


def github(args, pkgs, type="releases", field="tag_name"):
    res = {}
    for name, pkg in pkgs.items():
        id_ = pkg.get("github")
        if id_:
            arg_github_oauth = args["github_oauth"]
            headers = {}
            if arg_github_oauth:
                headers = {"Authorization": f"token {arg_github_oauth}"}
            r = requests.get(f"{github_base}/{id_}/{type}", headers=headers)
            j = json.loads(r.text)
            if j:
                vers = [x[field] for x in j if field in x]
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
        r = requests.get(f"{arch_base}/?name={id_}")
        j = json.loads(r.text)
        r = j["results"]
        if r:
            res[name] = try_parse_versions([r[0]["pkgver"]])[0]
    return res


def aur(args, pkgs):
    query = []
    items = list(pkgs.items())
    for name, pkg in items:
        id_ = pkg.get("aur", name)
        query.append(f"arg[]={id_}")
    query = "&".join(query)
    r = requests.get(f"{aur_base}/?v=5&type=info&{query}")
    j = json.loads(r.text)
    r = j["results"]
    res = {}
    for i, v in enumerate(r):
        if v:
            res[items[i][0]] = try_parse_versions([v["Version"]])[0]
    return res


sources_list = [github, gitlab, aur, arch]
sources = {x.__name__: x for x in sources_list}
sources["github_tags"] = github_tags
sources["gitlab_tags"] = gitlab_tags


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


def status(source, query, vers):
    eprint(f"Asking {source} for {len(query)} packages (current {len(vers)})")


def get_primary(args, c, vers):
    primary = [(k, v) for k, v in c.items() if "primary" in v]
    primary = sorted(primary, key=lambda i: i[1]["primary"])
    primary = groupby(primary, key=lambda i: i[1]["primary"])
    vers = dict(vers)
    asked = set()
    for k, g in primary:
        g = list(g)
        status(k, g, vers)
        asked.add(k)
        s = sources[k]
        vers = update(vers, s(args, dict(g)))
    return vers, asked


def get_secondary_source(args, c, s, vers, left):
    status(s.__name__, left, vers)
    vers = update(vers, s(args, left))
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
    return vers


@click.command()
@click.argument("config", type=click.File("r", encoding="UTF-8"))
@click.option("--github-oauth", type=click.STRING, help="github oauth token")
@click.option("--primary/--no-primary", default=True, help="query primary sources")
@click.option("--secondary/--no-secondary", default=True, help="query primary sources")
@click.option(
    "--trust-primary/--no-trust-primary", default=True, help="trust primary sources"
)
@click.option(
    "--trust-secondary/--no-trust-secondary",
    default=True,
    help="trust secondary sources",
)
def cli(config, **kwargs):
    c = toml.load(config)
    vers = get_vers(kwargs, c)
    print(json.dumps(make_serializable((vers))))


if __name__ == "__main__":
    cli()
