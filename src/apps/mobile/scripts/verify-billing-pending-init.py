#!/usr/bin/env python3
"""Run the native bridge's pending-purchases builder against a real Billing AAR.

Requires a JDK (javac/java) and the AAR matching app/build.gradle. No Android
account, emulator, network access, token or purchase is needed. --source supports
an isolated original file for regression/rollback verification.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile


def main() -> int:
    mobile = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aar", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=mobile / "android/app/src/main/java/com/foresightnine/mobile/ForesightNineBillingModule.kt")
    args = parser.parse_args()
    source = args.source.read_text(encoding="utf-8")
    expressions = re.findall(
        r"PendingPurchasesParams\s*\.\s*newBuilder\(\s*\)"
        r"(?:\s*\.\s*(?:enableOneTimeProducts|enablePrepaidPlans)\(\s*\))*"
        r"\s*\.\s*build\(\s*\)", source,
    )
    if len(expressions) != 1:
        raise SystemExit("Expected exactly one supported PendingPurchasesParams builder in native source.")
    expression = re.sub(r"\s+", "", expressions[0])
    version = re.search(r'com\.android\.billingclient:billing:([^"\s]+)', (mobile / "android/app/build.gradle").read_text()).group(1)
    with tempfile.TemporaryDirectory(prefix="f9-billing-init-") as temporary:
        root = Path(temporary)
        jar = root / "classes.jar"
        with zipfile.ZipFile(args.aar) as archive:
            jar.write_bytes(archive.read("classes.jar"))
        with zipfile.ZipFile(jar) as archive:
            builder = archive.read("com/android/billingclient/api/PendingPurchasesParams$Builder.class")
            if f"com.android.billingclient:billing@@{version}".encode() not in builder:
                raise SystemExit("AAR version does not match app/build.gradle.")
        java = root / "NativePendingParamsProbe.java"
        java.write_text(
            "import com.android.billingclient.api.PendingPurchasesParams;\n"
            "public final class NativePendingParamsProbe {\n"
            " public static void main(String[] args) {\n"
            f"  PendingPurchasesParams params = {expression};\n"
            '  if (params == null) throw new AssertionError("Missing pending parameters");\n'
            '  System.out.println("NATIVE_PENDING_INIT=PASS");\n'
            " }\n}\n", encoding="utf-8",
        )
        print(f"BILLING_VERSION={version}", flush=True)
        print(f"SOURCE_SHA256={hashlib.sha256(args.source.read_bytes()).hexdigest()}", flush=True)
        print(f"BUILDER={expression}", flush=True)
        subprocess.run(["javac", "-classpath", str(jar), "-d", str(root), str(java)], check=True)
        return subprocess.run(["java", "-classpath", os.pathsep.join([str(jar), str(root)]), "NativePendingParamsProbe"]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
