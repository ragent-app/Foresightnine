#!/usr/bin/env python3
import json
import sys
import urllib.error
import urllib.request
from datetime import timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account

SCOPE = 'https://www.googleapis.com/auth/androidpublisher'
PACKAGE = 'com.foresightnine.mobile'


def api_get(label, name, url, token):
    request = urllib.request.Request(
        url,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
        method='GET',
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
            print(f'{label}_{name}=HTTP_STATUS={response.status} RESULT=readable')
            return response.status
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        result = 'package_not_bootstrapped' if exc.code == 404 else 'permission_error' if exc.code in (401, 403) else 'http_error'
        print(f'{label}_{name}=HTTP_STATUS={exc.code} RESULT={result}')
        return exc.code
    except Exception as exc:
        print(f'{label}_{name}=ERROR_TYPE={type(exc).__name__} RESULT=transport_error')
        return None


def run(label, path):
    path = Path(path)
    try:
        with path.open('r', encoding='utf-8') as stream:
            document = json.load(stream)
        print(f'{label}_METADATA=PASS TYPE={document.get("type")} PROJECT_ID={document.get("project_id")} CLIENT_EMAIL={document.get("client_email")}')
    except Exception as exc:
        print(f'{label}_METADATA=FAIL ERROR_TYPE={type(exc).__name__}')
        return 1
    try:
        credentials = service_account.Credentials.from_service_account_file(str(path), scopes=[SCOPE])
        credentials.refresh(Request())
        expiry = credentials.expiry.astimezone(timezone.utc).isoformat() if credentials.expiry else 'absent'
        scope = 'androidpublisher' if SCOPE in set(credentials.scopes or ()) else 'unexpected'
        print(f'{label}_TOKEN_EXCHANGE=PASS HTTP_STATUS=200 SCOPE={scope} EXPIRY={expiry}')
    except Exception as exc:
        status = getattr(getattr(exc, 'response', None), 'status', 'unknown')
        print(f'{label}_TOKEN_EXCHANGE=FAIL HTTP_STATUS={status} ERROR_TYPE={type(exc).__name__}')
        return 1
    application_url = f'https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PACKAGE}'
    tracks_url = f'https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PACKAGE}/tracks/internal/releases'
    app_status = api_get(label, 'APPLICATION_READ', application_url, credentials.token)
    track_status = api_get(label, 'TRACK_READ', tracks_url, credentials.token)
    if app_status == 200:
        print(f'{label}_PLAY_API_AUTHENTICATION=PASS PACKAGE_STATUS=readable')
    elif app_status == 404:
        print(f'{label}_PLAY_API_AUTHENTICATION=PASS PACKAGE_STATUS=AUTHENTICATION_PASS_PACKAGE_NOT_BOOTSTRAPPED')
    elif app_status in (401, 403):
        print(f'{label}_PLAY_API_AUTHENTICATION=FAIL PACKAGE_STATUS=permission_error')
    else:
        print(f'{label}_PLAY_API_AUTHENTICATION=UNCONFIRMED')
    if track_status == 403:
        print(f'{label}_TRACK_READ_NOTE=PERMISSION_DENIED_WHILE_PACKAGE_BOOTSTRAP_CHECK_IS_404')
    return 0

if len(sys.argv) < 3 or len(sys.argv[1:]) % 2:
    raise SystemExit('usage: check_play_auth.py LABEL PATH [LABEL PATH ...]')
exit_code = 0
for index in range(1, len(sys.argv), 2):
    exit_code |= run(sys.argv[index], sys.argv[index + 1])
raise SystemExit(exit_code)
