# ForesightNine Android certificate inventory

Source archive: `source/certificates.zip`

The archive contains public `.der` certificates only. No private key is present or processed.

## Classification

- `deployment_cert.der`: Play App Signing/deployment candidate, selected because the supplied filename explicitly identifies it as the deployment certificate. SHA-256 is recorded in the QA evidence.
- `hybrid_classical_cert.der`: alternate/unknown certificate; not added to App Links because its role as an APK signing certificate is not established by the archive.
- `hybrid_pqc_cert.der`: alternate/unknown certificate; not added to App Links because its role as an APK signing certificate is not established by the archive.

The current QA APK signer is recorded separately from `deployment_cert.der` and is included for staging verification. The production Play signing certificate and QA certificate are both represented in generated staging assetlinks; the upload certificate is not used.

## Fingerprint inventory

| Input | SHA-256 fingerprint | App Link use |
|---|---|---|
| `deployment_cert.der` | `33:4B:79:0C:E2:40:5F:11:87:B9:3D:FB:64:2D:50:7C:48:A8:87:2F:9E:5E:10:E8:E4:9B:EC:30:A5:84:45:A9` | included as deployment candidate |
| `hybrid_classical_cert.der` | `CD:2B:E8:0A:81:BF:94:FE:6E:15:EC:5D:68:50:03:6D:0F:49:37:02:43:17:73:5A:A3:E6:E1:ED:99:82:BC:50` | retained; role unresolved |
| `hybrid_pqc_cert.der` | `83:91:43:0F:12:45:BE:4A:B2:90:79:FC:AE:17:51:42:DA:F7:06:B4:E2:4B:6F:36:3C:DE:5D:97:51:5B:65:6C` | retained; role unresolved |
| QA debug APK signer | `FA:C6:17:45:DC:09:03:78:6F:B9:ED:E6:2A:96:2B:39:9F:73:48:F0:BB:6F:89:9B:83:32:66:75:91:03:3B:9C` | included for staging verification |
