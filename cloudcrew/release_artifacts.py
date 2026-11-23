"""Build a release once, then check the exact files that will be published."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import site
from .core import ROOT, digest, now, redact, write_json
from .release_config import ReleaseConfig, object_path, release_id


PAGES = tuple(site.PAGES.values())
ALLOWED_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".txt", ".json"}
CONTENT_TYPES = {'.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml',
                 '.json': 'application/json', '.txt': 'text/plain', '.png': 'image/png', '.jpg': 'image/jpeg',
                 '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.ico': 'image/x-icon'}


def file_manifest(root: Path) -> dict:
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Release files cannot be symlinks")
        if path.is_file():
            name = object_path(path.relative_to(root).as_posix())
            if path.suffix.lower() not in ALLOWED_SUFFIXES or path.stat().st_size > 10_000_000:
                raise ValueError(f"Unsupported or oversized release file: {name}")
            data = path.read_bytes()
            if path.suffix.lower() in ('.html', '.js', '.css', '.svg', '.txt', '.json'):
                text = data.decode('utf-8')
                if redact(text) != text or re.search(r'\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b', text):
                    raise ValueError(f'Release file appears to contain a credential: {name}')
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                           "content_type": CONTENT_TYPES[path.suffix.lower()]}
    if not files or len(files) > 1000 or sum(v['size'] for v in files.values()) > 100_000_000:
        raise ValueError("Release is empty or exceeds the bounded size")
    return files


def package_site(source: Path, destination: Path, commit: str, config: ReleaseConfig, *, bootstrap=False) -> dict:
    release_id(commit)
    if commit.startswith("bootstrap-") and not bootstrap:
        raise ValueError("Only initial adoption can create a bootstrap release")
    if destination.exists():
        raise ValueError("Choose an empty release output directory")
    file_manifest(source)
    destination.mkdir(parents=True)
    shutil.copytree(source, destination / "site")
    metadata = {"schema_version": 1, "release_id": commit,
                "source_commit": None if bootstrap else commit, "binding": config.binding}
    for path in (destination / "site").glob("*.html"):
        text = path.read_text()
        text = re.sub(r'((?:src|href)=["\'])assets/', rf'\1/releases/{commit}/assets/', text)
        text = text.replace("</head>", f'<meta name="gbm-release" content="{commit}"></head>')
        text = text.replace("</footer>", f'<p class="release-label" data-release-id="{commit}">Release {commit[:12]}</p></footer>')
        path.write_text(text)
    write_json(destination / "site/release.json", metadata)
    manifest = {**metadata, "files": file_manifest(destination / "site")}
    manifest['manifest_digest'] = digest(manifest)
    write_json(destination / "manifest.json", manifest)
    report = validate_bundle(destination, config, expected_commit=commit)
    write_json(destination / "artifact-validation.json", report)
    if report['status'] != "passed":
        raise ValueError("Packaged release failed validation: " + "; ".join(report['errors'][:8]))
    return manifest


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.ids, self.refs, self.links = set(), set(), [], []
        self.version, self.h1, self.title, self.in_title = None, 0, "", False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.add(tag)
        if tag == "title": self.in_title = True
        if tag == "h1": self.h1 += 1
        if attrs.get("id"): self.ids.add(attrs['id'])
        if tag == "meta" and attrs.get("name") == "gbm-release": self.version = attrs.get("content")
        for key in ("href", "src"):
            if attrs.get(key): self.refs.append(attrs[key])
        if tag == "a" and attrs.get("href") in PAGES: self.links.append(attrs['href'])
        if tag == "img" and not attrs.get("alt"): raise ValueError("Image has no alternative text")

    def handle_endtag(self, tag):
        if tag == "title": self.in_title = False

    def handle_data(self, data):
        if self.in_title: self.title += data


def validate_manifest(manifest: dict, config: ReleaseConfig, expected_commit=None):
    version = release_id(manifest.get("release_id"))
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    if manifest.get('schema_version') != 1 or manifest.get('binding') != config.binding or digest(unsigned) != manifest.get('manifest_digest'):
        raise ValueError("Release manifest is invalid or belongs to another deployment")
    if expected_commit and version != expected_commit:
        raise ValueError("Validated release does not match the requested source commit")
    if manifest.get('source_commit') != (None if version.startswith('bootstrap-') else version):
        raise ValueError("Release identity and source commit disagree")
    files = manifest.get('files', {})
    if not isinstance(files, dict) or not 1 <= len(files) <= 1000:
        raise ValueError("Invalid release manifest file list")
    for name, info in files.items():
        object_path(name)
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES or not re.fullmatch(r"[a-f0-9]{64}", info.get('sha256', '')):
            raise ValueError("Invalid release file digest or type")
        if type(info.get('size')) is not int or not 0 <= info['size'] <= 10_000_000:
            raise ValueError("Invalid release file size")
        if info.get('content_type') != CONTENT_TYPES[Path(name).suffix.lower()]:
            raise ValueError('Invalid release content type')
    if sum(info['size'] for info in files.values()) > 100_000_000:
        raise ValueError('Release exceeds its total size limit')
    if not set(PAGES + ('404.html', 'release.json', 'assets/app.js', 'assets/styles.css')).issubset(files):
        raise ValueError("Required release files are missing")
    return version


def validate_bundle(bundle: Path, config: ReleaseConfig, expected_commit=None) -> dict:
    errors, pages = [], {}
    try:
        manifest = json.loads((bundle / "manifest.json").read_text())
        version = validate_manifest(manifest, config, expected_commit)
        actual = file_manifest(bundle / "site")
        if actual != manifest['files']:
            errors.append("Release bytes differ from the tested manifest")
        metadata = json.loads((bundle / "site/release.json").read_text())
        if any(metadata.get(k) != manifest.get(k) for k in ('release_id', 'source_commit', 'binding')):
            errors.append("Public release identifier differs from the manifest")
        for name in manifest['files']:
            if not name.endswith('.html'): continue
            page = Page()
            page.feed((bundle / "site" / name).read_text())
            pages[name] = page
            if not {'html', 'head', 'title', 'h1', 'main', 'nav', 'footer'}.issubset(page.tags) or not page.title.strip() or page.h1 != 1:
                errors.append(f"{name}: missing required document structure")
            if page.version != version or not set(PAGES).issubset(page.links):
                errors.append(f"{name}: missing release identity or page navigation")
        for name, page in pages.items():
            for ref in page.refs:
                url = urlsplit(ref)
                if url.scheme or url.netloc: continue
                target = unquote(url.path) or name
                prefix = f"/releases/{version}/"
                if target.startswith(prefix): target = target[len(prefix):]
                elif target.startswith('/'): target = target[1:]
                try: object_path(target)
                except ValueError:
                    errors.append(f"{name}: unsafe link {ref}"); continue
                if target not in manifest['files']:
                    errors.append(f"{name}: missing link or asset {ref}")
                if url.fragment and target in pages and url.fragment not in pages[target].ids:
                    errors.append(f"{name}: missing fragment {ref}")
                if target.startswith('assets/') and not url.path.startswith(prefix):
                    errors.append(f"{name}: asset URL is not versioned")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    return {"status": "failed" if errors else "passed", "timestamp": now(), "pages_checked": len(pages), "errors": errors}


def run_checks(config: ReleaseConfig, commit: str, output: Path, *, root: Path = ROOT, tests=True) -> dict:
    release_id(commit)
    report = {"status": "failed", "source_commit": commit, "timestamp": now(), "steps": []}
    output.mkdir(parents=True, exist_ok=True)
    try:
        if any(path.is_symlink() for path in (root / 'website').rglob('*')):
            raise ValueError('Website source cannot contain symlinks')
        if tests:
            commands = [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                        ["node", "--test", *[str(p.relative_to(root)) for p in sorted((root / "tests").glob('*.test.cjs'))]],
                        ["node", "--check", "website/assets/app.js"]]
            for command in commands:
                result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=240)
                record = {"command": command, "exit_code": result.returncode,
                          "status": "passed" if result.returncode == 0 else "failed",
                          "output": redact((result.stdout + result.stderr)[-24000:])}
                report['steps'].append(record)
                if result.returncode: raise ValueError("Mandatory automated test failed")
        build = output / "unversioned"
        site.build(build)
        checked = site.check(build)
        report['steps'].append({"tool": "static_site_check", **checked})
        if checked['status'] != 'passed': raise ValueError("Mandatory website validation failed")
        manifest = package_site(build, output / "release", commit, config)
        report.update(status="passed", manifest_digest=manifest['manifest_digest'], release_id=commit)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report['error'] = redact(str(exc))
    finally:
        write_json(output / "validation-report.json", report)
    return report
