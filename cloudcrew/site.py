from __future__ import annotations

import html
import json
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

from .core import ROOT

BRAND = "GBM Cloud"
PAGES = {"home": "index.html", "solutions": "solutions.html", "resources": "resources.html", "about": "about.html", "planner": "planner.html"}
LABELS = {"home": "Home", "solutions": "Solutions", "resources": "Resources", "about": "About", "planner": "Plan your launch"}
# Existing bookmarks render current content with canonical navigation.
LEGACY_PAGES = {"trails.html": "solutions", "journal.html": "resources", "plan.html": "planner"}
LEGACY_KEYS = {"trails": "solutions", "journal": "resources", "plan": "planner"}
SOLUTIONS = [
    {"name": "Static website", "category": "web", "level": "beginner", "icon": "window", "tag": "MULTI-PAGE WEBSITES", "description": "Bring your pages together with a custom domain, HTTPS, and content delivered from a private origin.", "services": ["S3", "CloudFront", "Route 53", "ACM"]},
    {"name": "Portfolio launch", "category": "web", "level": "beginner", "icon": "layers", "tag": "PERSONAL PROJECTS", "description": "Give your work a home of its own. A lightweight foundation for portfolios, project showcases, and personal sites.", "services": ["S3", "CloudFront", "Route 53", "ACM"]},
    {"name": "Resource library", "category": "content", "level": "beginner", "icon": "folder", "tag": "DOCUMENTS & GUIDES", "description": "Organize a collection of guides and downloadable resources behind a clear, easy-to-navigate static experience.", "services": ["S3", "CloudFront"]},
    {"name": "Media delivery", "category": "content", "level": "intermediate", "icon": "image", "tag": "IMAGES & STATIC ASSETS", "description": "Explore edge delivery and thoughtful caching for an image-rich experience without a continuously running server.", "services": ["S3", "CloudFront"]},
    {"name": "Domain & HTTPS", "category": "security", "level": "beginner", "icon": "globe", "tag": "A TRUSTED FRONT DOOR", "description": "Connect the dots between domain records, certificate validation, and secure delivery through CloudFront.", "services": ["Route 53", "ACM", "CloudFront"]},
    {"name": "Private origin", "category": "security", "level": "intermediate", "icon": "shield", "tag": "ACCESS BY DESIGN", "description": "Understand origin access control and distribution-scoped permissions that keep direct anonymous S3 access closed.", "services": ["S3", "CloudFront"]},
]


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def icon(name: str, css: str = "") -> str:
    paths = {
        "cloud": '<path d="M7 18a5 5 0 0 1-.6-10A6 6 0 0 1 18 7a5.5 5.5 0 0 1 .5 11Z"/>',
        "arrow": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
        "diagonal": '<path d="M6 18 18 6M6 6h12v12"/>',
        "check": '<path d="m5 12 4 4L19 6"/>',
        "window": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01M7 13h4m-4 3h8"/>',
        "layers": '<path d="m12 3 10 5-10 5L2 8Zm-9 10 9 5 9-5M3 18l9 5 9-5"/>',
        "folder": '<path d="M3 8V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M8 12h8m-8 4h5"/>',
        "image": '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
        "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z"/>',
        "shield": '<path d="m12 3 8 3v6c0 4-5 8-8 9-3-1-8-5-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
        "book": '<path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1Zm0 0v15"/>',
        "download": '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
        "reset": '<path d="M4 11a8 8 0 1 1 2 7M4 4v7h7"/>',
        "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    }
    return f'<svg class="icon {css}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{paths[name]}</svg>'


def solution_card(solution: dict, index: int, filtered: bool = False) -> str:
    attributes = f'data-solution data-category="{solution["category"]}" data-level="{solution["level"]}"' if filtered else ""
    query = esc(urlencode({"solution": solution["name"]}))
    return f'''<article class="solution-card" {attributes}>
      <div class="card-top"><span class="solution-icon">{icon(solution['icon'])}</span><span class="level-badge">{solution['level'].title()}</span></div>
      <p class="eyebrow">{solution['tag']}</p><h3>{solution['name']}</h3><p class="card-description">{solution['description']}</p>
      <div class="service-tags" aria-label="Services in this blueprint">{''.join('<span>' + service + '</span>' for service in solution['services'])}</div>
      <a class="card-link" href="planner.html?{query}">Plan this project {icon('diagonal')}</a></article>'''


def intro(kicker: str, title: str, description: str) -> str:
    return f'<section class="page-intro wrap"><p class="eyebrow">{kicker}</p><h1>{esc(title)}</h1><p class="lead">{esc(description)}</p></section>'


def body_for(page: str, content: dict) -> str:
    title, description = content["title"], content["intro"]
    if page == "home":
        return f'''<section class="hero"><div class="hero-inner wrap"><div class="hero-copy"><p class="eyebrow"><span class="eyebrow-line"></span> MAKE ROOM FOR WHAT’S NEXT</p>
        <h1>A clearer path<br>to <span>the cloud.</span></h1><p class="lead">{esc(description)}</p><div class="hero-actions"><a class="button" href="solutions.html">Explore solutions {icon('arrow')}</a><a class="button button-quiet" href="resources.html#architecture">See how it works {icon('diagonal')}</a></div>
        <div class="hero-details"><span>{icon('check')} Practical blueprints</span><span>{icon('check')} Privacy-first patterns</span><span>{icon('check')} Built to learn</span></div></div>
        <figure class="hero-visual"><div class="visual-heading"><span class="mini-brand">{icon('cloud')} GBM Cloud</span><span>REFERENCE ARCHITECTURE</span></div><img src="assets/cloud-architecture.svg" alt="A visitor connects over HTTPS to CloudFront, which delivers files from a private S3 origin. Route 53 handles DNS and ACM provides the certificate."><figcaption><span class="small-dot"></span> A simple foundation. Thoughtfully connected.</figcaption></figure></div></section>
        <section class="service-strip" aria-label="Built around approved AWS services"><div class="wrap strip-inner"><p>A focused foundation.<br><strong>Four services that work together.</strong></p><div><span>{icon('folder')} Amazon S3</span><span>{icon('globe')} CloudFront</span><span>{icon('layers')} Route 53</span><span>{icon('shield')} Certificate Manager</span></div></div></section>
        <section class="section wrap"><div class="section-heading"><div><p class="eyebrow">FIND YOUR STARTING POINT</p><h2>Good ideas deserve<br>a solid foundation.</h2></div><div class="section-heading-aside"><p>Start with a focused blueprint.<br>Shape it around what you want to build.</p><a class="text-link" href="solutions.html">View all solutions {icon('arrow')}</a></div></div><div class="solution-grid">{''.join(solution_card(SOLUTIONS[i], i) for i in (0, 2, 5))}</div></section>
        <section class="process-section"><div class="wrap process-layout"><div><p class="eyebrow">LESS GUESSWORK. MORE UNDERSTANDING.</p><h2>Know what you’re<br>building. And why.</h2><p class="lead">The cloud feels simpler when the pieces make sense. Take it one considered step at a time.</p><a class="text-link" href="resources.html">Explore the resource hub {icon('arrow')}</a><img class="process-art" src="assets/cloud-network.svg" alt="Connected cloud components with a maroon path linking them" loading="lazy"></div><ol class="process-steps"><li><span class="step-number">01</span><div><h3>Explore a blueprint</h3><p>Find a clear starting point for a website, content library, or secure delivery pattern.</p></div></li><li><span class="step-number">02</span><div><h3>Make the connections</h3><p>Understand how storage, delivery, DNS, and certificates fit together.</p></div></li><li><span class="step-number">03</span><div><h3>Launch with intention</h3><p>Build a checklist, review the details, and verify the experience you put online.</p></div></li></ol></div></section>
        <section class="section wrap resource-feature"><div class="resource-art"><img src="assets/cloud-security.svg" alt="A shield protects a cloud and its connected content" loading="lazy"><span>THE GBM CLOUD RESOURCE HUB</span></div><div><p class="eyebrow">A LITTLE KNOWLEDGE GOES A LONG WAY</p><h2>Confidence starts<br>with clarity.</h2><p class="lead">What makes an origin private? Where does HTTPS fit? Get useful explanations for the decisions behind a better launch.</p><a class="text-link" href="resources.html#security">Read about secure delivery {icon('arrow')}</a></div></section>
        {callout()}'''
    if page == "solutions":
        return intro("A PRACTICAL PLACE TO BEGIN", title, description) + f'''<section class="wrap catalog-section"><div class="filter-bar"><div class="filter-control"><label for="solution-category">What are you building?</label><select id="solution-category"><option value="all">All categories</option><option value="web">Web experiences</option><option value="content">Content delivery</option><option value="security">Domain & security</option></select></div><div class="filter-control"><label for="solution-level">Your starting level</label><select id="solution-level"><option value="all">All levels</option><option value="beginner">Beginner</option><option value="intermediate">Intermediate</option></select></div><p id="solution-count" role="status" aria-live="polite">6 solutions to explore</p><button type="button" class="text-link" id="reset-filters">Reset filters {icon('reset')}</button></div>
        <div class="solution-grid" id="solution-results">{''.join(solution_card(s, i, True) for i, s in enumerate(SOLUTIONS))}</div><div class="empty-state" id="solution-empty" hidden>{icon('cloud')}<h2>A different combination might help.</h2><p>No solutions match those filters. Choose another category or reset the filters to see everything.</p></div>
        <aside class="notice">{icon('book')}<div><strong>Blueprints for understanding, plans for your own project.</strong><p>These are educational starting points built around the approved AWS services. Use the launch planner to adapt a checklist to your priorities.</p></div></aside></section>{callout()}'''
    if page == "resources":
        return intro("THE RESOURCE HUB", title, description) + f'''<div class="wrap reading-layout"><aside class="reading-sidebar"><span class="eyebrow">IN THIS GUIDE</span><nav aria-label="Resource articles"><a href="#architecture"><span>01</span> The architecture</a><a href="#launch"><span>02</span> Before you launch</a><a href="#security"><span>03</span> Private by design</a></nav><a class="sidebar-cta" href="planner.html">Put it into a plan {icon('arrow')}</a></aside><div class="articles">
        <article id="architecture"><span id="packing" class="legacy-anchor" aria-hidden="true"></span><div class="article-visual"><img src="assets/cloud-architecture.svg" alt="CloudFront delivers files from S3, with Route 53 DNS and an ACM certificate"></div><p class="eyebrow">01 / UNDERSTAND THE CONNECTIONS</p><h2>A simple architecture, explained.</h2><p>A static website can be a collection of HTML, CSS, JavaScript, and images. It can still support useful interactions in the browser without a continuously running backend server.</p><p>Amazon S3 stores the files. CloudFront delivers them to visitors. Route 53 connects a custom domain to the distribution, and AWS Certificate Manager supplies its TLS certificate. Together, they create a focused foundation for a public website.</p><p>With origin access control, CloudFront signs its requests to a regular S3 origin. The bucket can stay private while the website is available through CloudFront.</p><a class="text-link" href="solutions.html">Find a blueprint {icon('arrow')}</a></article>
        <article id="launch"><span id="pace" class="legacy-anchor" aria-hidden="true"></span><p class="eyebrow">02 / REVIEW THE SMALL THINGS</p><h2>Before you share the link.</h2><p>Open every page, follow the navigation, and check your site on a smaller screen. Try the interactive features, test a missing address, and make sure the page remains useful when a visitor takes an unexpected path.</p><p>Then look at the delivery path. Does HTTP redirect to HTTPS? Does the certificate match the domain? Are the expected files reaching the browser? Record what you actually observe, so a successful launch is something you can explain.</p><p>The <a href="planner.html">launch planner</a> turns these considerations into a checklist you can save on your device and download.</p></article>
        <article id="security"><span id="care" class="legacy-anchor" aria-hidden="true"></span><p class="eyebrow">03 / PRIVATE BY DESIGN</p><h2>A public website. A private origin.</h2><p>Public access to a website does not require public access to its storage bucket. Block anonymous S3 access and allow CloudFront to read the objects through origin access control.</p><p>Scope the bucket policy to the intended CloudFront distribution, use HTTPS, and keep credentials outside your website files and source repository. Treat permissions as part of the design, then test that a direct anonymous S3 request is denied.</p><p>For the underlying configuration, see <a href="https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html" target="_blank" rel="noopener noreferrer">AWS’s guide to origin access control {icon('diagonal')}</a>.</p></article></div></div>{callout()}'''
    if page == "about":
        return intro("PURPOSE BEFORE COMPLEXITY", title, description) + f'''<section class="wrap story-grid"><div class="story-visual"><img src="assets/cloud-network.svg" alt="An illustrated cloud connects a set of modular services"><div class="story-stamp">{icon('cloud')}<span>ONE CONNECTED IDEA.<br>ROOM TO BUILD WHAT’S NEXT.</span></div></div><div><p class="eyebrow">A MORE APPROACHABLE CLOUD</p><h2>Built to make<br>the pieces click.</h2><p>GBM Cloud is a student-built cloud learning experience created for CSCE 412 at Texas A&M. It brings together practical AWS blueprints, concise explanations, and a browser-based launch planner.</p><p>The idea is simple: make thoughtful infrastructure easier to understand. Start with a focused use case, learn what each service contributes, and work toward a launch you can verify and explain.</p><p class="muted">Our maroon and white visual identity takes inspiration from Texas A&M. GBM Cloud is an independent student project.</p><a class="text-link" href="resources.html">Explore the thinking behind it {icon('arrow')}</a></div></section>
        <section class="section wrap"><div class="section-heading"><div><p class="eyebrow">WHAT GUIDES US</p><h2>A few principles.<br>A stronger foundation.</h2></div></div><div class="values"><article><span>01 /</span>{icon('layers')}<h3>Keep the purpose clear.</h3><p>Choose the pieces a project needs, and understand the role each one plays.</p></article><article><span>02 /</span>{icon('shield')}<h3>Make care part of the design.</h3><p>Think about access, HTTPS, permissions, and recovery from the beginning.</p></article><article><span>03 /</span>{icon('check')}<h3>Verify what you build.</h3><p>Use real observations to connect an intended architecture with its behavior.</p></article></div></section>{callout()}'''
    if page == "planner":
        return intro("YOUR NEXT LAUNCH, A LITTLE CLEARER", title, description) + f'''<section class="wrap planner-layout"><form id="launch-planner" class="planner-panel"><div class="panel-heading"><span class="solution-icon">{icon('layers')}</span><div><p class="eyebrow">MAKE IT YOUR OWN</p><h2>Your project details.</h2></div></div><label for="project-name">Project name</label><input id="project-name" name="project" maxlength="80" autocomplete="off" placeholder="My next cloud project"><div class="form-row"><div><label for="project-scope">Where are you headed?</label><select id="project-scope" name="scope"><option value="prototype">An early prototype</option><option value="launch">A public launch</option></select></div><div><label for="project-focus">What matters most?</label><select id="project-focus" name="focus"><option value="balanced">A balanced foundation</option><option value="performance">Performance & delivery</option><option value="security">Security & access</option></select></div></div><button class="button" type="submit">Build my launch checklist {icon('arrow')}</button><p class="caption">Your plan stays in this browser. Download a copy to take it with you.</p><p id="planner-status" role="status" aria-live="polite"></p></form>
        <div class="checklist-panel"><div class="checklist-heading"><div><p class="eyebrow">ONE GOOD STEP AT A TIME</p><h2 id="list-title">Your launch checklist.</h2></div><span id="checklist-progress" role="status" aria-live="polite">0 / 8 ready</span></div><div class="progress-track" aria-hidden="true"><span id="progress-fill"></span></div><ul id="launch-checklist" class="checklist"></ul><div class="checklist-actions"><button type="button" class="text-link" id="download-list">{icon('download')} Download checklist</button><button type="button" class="text-link" id="clear-plan">{icon('reset')} Clear saved plan</button></div><noscript><p>Enable JavaScript to generate and save your checklist. Review pages and navigation, keep credentials private, configure your origin and HTTPS, and record a deployment and recovery plan.</p></noscript></div></section>
        <aside class="wrap notice">{icon('shield')}<div><strong>A planning tool, with you in control.</strong><p>This checklist helps organize your review. It does not create AWS resources or validate a live deployment. Adapt it to the requirements of your project.</p></div></aside>'''
    raise ValueError("Unknown page")


def callout() -> str:
    return f'''<section class="wrap callout-wrap"><div class="callout"><div><p class="eyebrow">YOUR NEXT IDEA STARTS HERE</p><h2>Make the next step a clear one.</h2><p>A name, a few priorities, and a plan you can work with.</p></div><a class="button" href="planner.html">Plan your launch {icon('arrow')}</a></div></section>'''


def shell(page: str, title: str, description: str, body: str) -> str:
    nav = "".join(f'<a href="{filename}"' + (' aria-current="page"' if key == page else '') + (' class="nav-cta"' if key == 'planner' else '') + f'>{LABELS[key]}' + (icon('diagonal') if key == 'planner' else '') + '</a>' for key, filename in PAGES.items())
    footer_nav = "".join(f'<a href="{filename}">{LABELS[key]}</a>' for key, filename in PAGES.items())
    brand = f'<span class="brand-symbol">{icon("cloud")}</span><span class="brand-wordmark">GBM <span>Cloud</span></span>'
    canonical = PAGES.get(page, "404.html")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} | GBM Cloud</title><meta name="description" content="{esc(description)}"><meta name="application-name" content="GBM Cloud"><meta name="theme-color" content="#6b293d"><meta property="og:site_name" content="GBM Cloud"><meta property="og:title" content="{esc(title)} | GBM Cloud"><meta property="og:description" content="{esc(description)}"><meta property="og:type" content="website"><link rel="canonical" href="{canonical}"><link rel="icon" href="assets/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="assets/styles.css"><script src="assets/app.js" defer></script></head>
<body><a class="skip-link" href="#main">Skip to content</a><div class="announcement"><div class="wrap"><span>Thoughtful cloud foundations. Built with an Aggie spirit.</span><a href="resources.html">A little clarity, right this way {icon('arrow')}</a></div></div><header class="site-header wrap"><a class="brand" href="index.html" aria-label="GBM Cloud home">{brand}</a><button class="menu-toggle" aria-controls="main-nav" aria-expanded="false" aria-label="Open navigation" type="button">Menu {icon('menu')}</button><nav id="main-nav" aria-label="Main navigation">{nav}</nav></header>
<main id="main">{body}</main><footer class="site-footer"><div class="wrap footer-top"><div><a class="brand" href="index.html" aria-label="GBM Cloud home">{brand}</a><p>Good ideas. Thoughtful foundations.<br>A clearer path to the cloud.</p></div><nav aria-label="Footer navigation">{footer_nav}</nav></div><div class="wrap footer-bottom"><span>© 2026 GBM Cloud. A student-built project for CSCE 412.</span><span>Independent project · Texas A&M-inspired identity</span></div></footer></body></html>'''


def build(output: Path = ROOT / "dist") -> dict:
    content = json.loads((ROOT / "website" / "content.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    assets = output / "assets"
    if assets.is_symlink():
        raise ValueError("Build assets directory cannot be a symlink")
    if assets.exists():
        shutil.rmtree(assets)
    shutil.copytree(ROOT / "website" / "assets", assets)
    for page, filename in PAGES.items():
        item = content[page]
        (output / filename).write_text(shell(page, item["title"], item["intro"], body_for(page, item)), encoding="utf-8")
    for filename, page in LEGACY_PAGES.items():
        item = content[page]
        (output / filename).write_text(shell(page, item["title"], item["intro"], body_for(page, item)), encoding="utf-8")
    error = f'''<section class="wrap error-page" data-error-page="404"><div class="error-art">{icon('cloud')}</div><p class="eyebrow">404 / PAGE NOT FOUND</p><h1>A small detour.<br>A clear way back.</h1><p class="lead">We couldn’t find that page. Head back to GBM Cloud, or explore a different starting point.</p><div class="hero-actions"><a class="button" href="index.html">Back to home {icon('arrow')}</a><a class="button button-quiet" href="solutions.html">Explore solutions {icon('diagonal')}</a></div></section>'''
    (output / "404.html").write_text(shell("error", "Page not found", "Find your way back to GBM Cloud.", error), encoding="utf-8")
    (output / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
    return {"status": "built", "brand": BRAND, "pages": list(PAGES.values()) + ["404.html"], "compatibility_pages": list(LEGACY_PAGES), "output": str(output)}


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.ids, self.nav, self.tags = [], set(), [], []
        self.in_main_nav = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "nav" and attrs.get("id") == "main-nav":
            self.in_main_nav = True
        if tag == "a" and self.in_main_nav:
            self.nav.append(attrs.get("href"))
        for key in ("href", "src"):
            if attrs.get(key):
                self.refs.append(attrs[key])
        if tag == "img" and not attrs.get("alt"):
            raise ValueError("Every content image needs alternative text")

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_main_nav = False


def check(root: Path = ROOT / "dist") -> dict:
    errors, pages = [], {}
    for name in list(PAGES.values()) + ["404.html"] + list(LEGACY_PAGES):
        try:
            parser = LinkParser()
            source = (root / name).read_text()
            parser.feed(source)
            pages[name] = parser
            for tag in ("title", "h1", "main", "nav"):
                if tag not in parser.tags:
                    errors.append(f"{name}: missing {tag}")
            if parser.nav != list(PAGES.values()):
                errors.append(f"{name}: inconsistent main navigation")
            if "GBM Cloud" not in source or any(old in source.lower() for old in ("field notes", "field-notes", "cloudcrew", "outdoor club")):
                errors.append(f"{name}: inconsistent branding")
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
    resolved = root.resolve()
    for name, parser in pages.items():
        for ref in parser.refs:
            parts = urlsplit(ref)
            if parts.scheme or parts.netloc:
                continue
            target = (root / unquote(parts.path or name)).resolve()
            if not target.is_relative_to(resolved) or not target.is_file():
                errors.append(f"{name}: missing or unsafe local resource {ref}")
            elif parts.fragment and target.suffix == ".html":
                target_parser = pages.get(target.name)
                if target_parser and parts.fragment not in target_parser.ids:
                    errors.append(f"{name}: broken fragment {ref}")
    return {"status": "passed" if not errors else "failed", "pages_checked": len(pages), "errors": errors,
            "scope": "Static HTML, branding, navigation, compatibility pages, local links, fragments and image alt text; not a browser or cloud verification."}


def update_intro(page: str, text: str) -> dict:
    page = LEGACY_KEYS.get(page, page)
    if page not in PAGES or not 30 <= len(text) <= 350:
        raise ValueError("Use a known page and 30–350 characters of plain text")
    path = ROOT / "website" / "content.json"
    content = json.loads(path.read_text())
    content[page]["intro"] = text
    path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    return {"page": page, "intro": text, "next": "Rebuild and check the website"}
