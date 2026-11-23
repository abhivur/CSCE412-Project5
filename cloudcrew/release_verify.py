"""Verify the public release, including actual browser behavior."""
from __future__ import annotations

import json
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from .core import ROOT, now, redact
from .release_artifacts import validate_manifest
from .verify import NoRedirect, fetch, tls_evidence


def browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        raise RuntimeError("Browser verification is required. Install requirements-ci.txt and run 'python3 -m playwright install chromium'. " + str(exc)[:300]) from exc


def planner_check(domain, evidence_dir=None):
    from playwright.sync_api import sync_playwright, expect
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = None
        screenshot = None
        screenshot_error = None
        def capture(label):
            nonlocal screenshot, screenshot_error
            try:
                directory = Path(evidence_dir or ROOT / 'out/browser')
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f'planner-{label}-{uuid.uuid4().hex}.png'
                page.screenshot(path=str(path), full_page=True)
                screenshot = str(path.relative_to(ROOT / 'out')) if path.is_relative_to(ROOT / 'out') else str(path)
            except Exception as exc:
                screenshot_error = redact(str(exc))
        try:
            context = browser.new_context(ignore_https_errors=False, accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            response = page.goto(f"https://{domain}/planner.html", wait_until='networkidle', timeout=30000)
            assert response and response.status == 200, "Planner response is not HTTP 200"
            page.locator('#project-name').fill('Production verification')
            page.locator('#project-scope').select_option('launch')
            page.locator('#project-focus').select_option('security')
            page.locator('#launch-planner button[type=submit]').click()
            expect(page.locator('#list-title')).to_contain_text('Production verification')
            boxes = page.locator('#launch-checklist input[type=checkbox]')
            expect(boxes).to_have_count(13)
            boxes.first.check()
            expect(page.locator('#checklist-progress')).to_contain_text('1 / 13')
            page.reload(wait_until='networkidle')
            expect(page.locator('#launch-checklist input:checked')).to_have_count(1)
            with page.expect_download() as waiting:
                page.locator('#download-list').click()
            download = waiting.value
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'checklist.txt'
                download.save_as(path)
                content = path.read_text()
                assert 'GBM Cloud' in content and 'Production verification' in content, "Downloaded checklist is wrong"
            page.locator('#clear-plan').click()
            expect(page.locator('#launch-checklist input:checked')).to_have_count(0)
            assert not errors, 'Browser errors: ' + '; '.join(errors)
            capture('passed')
            return {"status": "passed", "url": f"https://{domain}/planner.html",
                    "checks": ["form submission", "13-item checklist", "completion progress", "persistence", "download", "reset"],
                    "browser": browser.version, "page_errors": errors, "screenshot": screenshot, "screenshot_error": screenshot_error}
        except Exception as exc:
            if page: capture('failed')
            return {'status': 'failed', 'url': f'https://{domain}/planner.html', 'error': redact(str(exc)),
                    'page_errors': errors, 'screenshot': screenshot, 'screenshot_error': screenshot_error}
        finally:
            browser.close()


def read_public_release(domain):
    request = urllib.request.Request(f"https://{domain}/release.json", headers={"Cache-Control": "no-cache"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        return json.loads(response.read(16384))


def verify_legacy(config, manifest, aws):
    checks = []
    def check(name, action, predicate):
        try:
            result = action()
            checks.append({'name': name, 'passed': bool(predicate(result)), 'observation': result})
        except Exception as exc:
            checks.append({'name': name, 'passed': False, 'error': redact(str(exc))})
    base = f"https://{config['domain']}/"
    check('legacy_tls', lambda: tls_evidence(config['domain']), lambda r: r['hostname_verified'] and r['chain_verified'])
    for name, expected in manifest.items():
        check('legacy_file:' + name, lambda name=name: fetch(base + name),
              lambda r, expected=expected: r['status_code'] == 200 and r['body_sha256'] == expected)
    check('legacy_root', lambda: fetch(base), lambda r: r['status_code'] == 200 and r['body_sha256'] == manifest['index.html'])
    check('legacy_planner', lambda: planner_check(config['domain']), lambda r: r['status'] == 'passed')
    return {'status': 'passed' if checks and all(c['passed'] for c in checks) else 'failed', 'checks': checks}


def verify_production(config, manifest, aws, *, browser=planner_check, fetcher=fetch,
                      tls=tls_evidence, public_release=read_public_release):
    version = validate_manifest(manifest, config)
    checks = []

    def check(name, action, predicate=lambda result: bool(result)):
        try:
            observed = action()
            passed = bool(predicate(observed))
            checks.append({"name": name, "passed": passed, "observation": observed})
        except Exception as exc:
            checks.append({"name": name, "passed": False, "error": redact(str(exc))})

    base = f"https://{config['domain']}"
    check('certificate_and_tls', lambda: tls(config['domain']),
          lambda r: r['hostname_verified'] and r['chain_verified'] and r['protocol'] in ('TLSv1.2', 'TLSv1.3'))
    check('http_redirect', lambda: fetcher(f"http://{config['domain']}/"),
          lambda r: r['status_code'] in (301, 302, 307, 308) and r['headers'].get('location') == base + '/')
    check('release_identity', lambda: public_release(config['domain']),
          lambda r: r.get('release_id') == version and r.get('source_commit') == manifest['source_commit'] and r.get('binding') == config.binding)
    for name, info in manifest['files'].items():
        url = f"{base}/releases/{version}/{name}" if name.startswith('assets/') else f"{base}/{name}"
        check('file:' + name, lambda url=url: fetcher(url),
              lambda r, info=info: r['status_code'] == 200 and r['body_sha256'] == info['sha256'])
    check('root_page', lambda: fetcher(base + '/'),
          lambda r: r['status_code'] == 200 and r['body_sha256'] == manifest['files']['index.html']['sha256']
          and 'cloudfront' in r['headers'].get('via', '').lower() and bool(r['headers'].get('x-amz-cf-id')))
    check('custom_404', lambda: fetcher(base + '/missing-' + uuid.uuid4().hex + '.html'),
          lambda r: r['status_code'] == 404 and r.get('custom_404_present') is True)
    check('anonymous_s3_denied', lambda: fetcher(f"https://{config['site_bucket']}.s3.{config['region']}.amazonaws.com/releases/{version}/index.html"),
          lambda r: r['status_code'] == 403)
    check('s3_public_access_block', lambda: aws.call('s3api', 'get-public-access-block', {'Bucket': config['site_bucket']}),
          lambda r: all(r['PublicAccessBlockConfiguration'].get(k) is True for k in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')))
    check('s3_policy_not_public', lambda: aws.call('s3api', 'get-bucket-policy-status', {'Bucket': config['site_bucket']}),
          lambda r: r['PolicyStatus']['IsPublic'] is False)
    check('distribution_scoped_policy', lambda: json.loads(aws.call('s3api', 'get-bucket-policy', {'Bucket': config['site_bucket']})['Policy']),
          lambda r: any(s.get('Effect') == 'Allow' and s.get('Principal') == {'Service': 'cloudfront.amazonaws.com'}
                        and s.get('Action') == 's3:GetObject' and s.get('Condition', {}).get('StringEquals', {}).get('AWS:SourceArn') == config.distribution_arn
                        for s in r.get('Statement', [])))
    check('distribution_release', lambda: aws.call('cloudfront', 'get-distribution', {'Id': config['distribution_id']}),
          lambda r: distribution_matches(r, config, version))
    def signing():
        dist = aws.call('cloudfront', 'get-distribution-config', {'Id': config['distribution_id']})['DistributionConfig']
        target = dist['DefaultCacheBehavior']['TargetOriginId']
        origin = next(o for o in dist['Origins']['Items'] if o['Id'] == target)
        return aws.call('cloudfront', 'get-origin-access-control', {'Id': origin['OriginAccessControlId']})
    check('oac_always_signs', signing,
          lambda r: r['OriginAccessControl']['OriginAccessControlConfig']['SigningBehavior'] == 'always'
          and r['OriginAccessControl']['OriginAccessControlConfig']['SigningProtocol'] == 'sigv4')
    check('critical_planner_function', lambda: browser(config['domain']), lambda r: r.get('status') == 'passed')
    return {'schema_version': 1, 'timestamp': now(), 'release_id': version, 'source_commit': manifest['source_commit'],
            'status': 'passed' if all(c['passed'] for c in checks) else 'failed',
            'passed': sum(c['passed'] for c in checks), 'total': len(checks), 'checks': checks,
            'evidence_type': 'actual_public_http_tls_browser_and_aws_reads'}


def distribution_matches(result, config, version):
    dist = result['Distribution']
    cfg = dist['DistributionConfig']
    target = cfg['DefaultCacheBehavior']['TargetOriginId']
    origin = next(o for o in cfg['Origins']['Items'] if o['Id'] == target)
    behavior = next((b for b in cfg.get('CacheBehaviors', {}).get('Items', []) if b['PathPattern'] == 'releases/*'), None)
    archive = next((o for o in cfg['Origins']['Items'] if behavior and o['Id'] == behavior['TargetOriginId']), None)
    expected_host = f"{config['site_bucket']}.s3.{config['region']}.amazonaws.com"
    if not (dist['Status'] == 'Deployed' and cfg['Enabled'] and cfg['DefaultRootObject'] == 'index.html'
            and config['domain'] in cfg['Aliases'].get('Items', [])
            and cfg['DefaultCacheBehavior']['ViewerProtocolPolicy'] == 'redirect-to-https'
            and cfg['ViewerCertificate'].get('MinimumProtocolVersion') == 'TLSv1.2_2021'
            and origin.get('OriginPath') == '/releases/' + version and origin['DomainName'] == expected_host
            and archive and archive.get('OriginPath', '') == '' and archive['DomainName'] == expected_host
            and origin.get('OriginAccessControlId') and archive.get('OriginAccessControlId') == origin.get('OriginAccessControlId')):
        return False
    return True


def verify_with_retries(config, manifest, aws, audit, *, verifier=verify_production, sleep=time.sleep):
    result = None
    for attempt in range(config['verify_attempts']):
        result = verifier(config, manifest, aws)
        audit.artifact(f"production-check-{audit.seq:04d}.json", result)
        audit.emit('release_verifier', 'production_verification', {'attempt': attempt + 1, 'report': result})
        if result['status'] == 'passed': return result
        if attempt + 1 < config['verify_attempts']: sleep(config['verify_delay_seconds'])
    return result
