#!/bin/zsh
# Puts the code signing certificate into a temporary keychain for packaging/build.sh.
#   MEDIATIDY_SIGN_P12 (base64 of the .p12) and MEDIATIDY_SIGN_PASSWORD must be set.
# Prints the variables for build.sh (CI appends them to $GITHUB_ENV). The login keychain stays untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${MEDIATIDY_SIGN_P12:?}" "${MEDIATIDY_SIGN_PASSWORD:?}"

dir=$(mktemp -d)
keychain=$dir/mediatidy-signing.keychain-db
password=$(openssl rand -hex 24)
print -rn -- "$MEDIATIDY_SIGN_P12" | base64 --decode > "$dir/cert.p12"
security create-keychain -p "$password" "$keychain"
security set-keychain-settings -lut 3600 "$keychain"
security unlock-keychain -p "$password" "$keychain"
security import "$dir/cert.p12" -k "$keychain" -P "$MEDIATIDY_SIGN_PASSWORD" -f pkcs12 -T /usr/bin/codesign >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$password" "$keychain" >/dev/null
rm -f "$dir/cert.p12"
# codesign also looks up the certificate through the search list; on your own Mac leave that list alone
if [[ -n ${CI:-} ]]; then
  security list-keychains -d user -s "$keychain" ${(f)"$(security list-keychains -d user | tr -d ' "')"}
fi

# The certificate is self-signed and therefore "not trusted" – codesign still uses it by its SHA-1
identity=$(sed -n 's/^SHA-1: *//p' packaging/signing-certificate.txt)
security find-identity -p codesigning "$keychain" | grep -q "$identity" \
  || { print -u2 "ERROR: certificate $identity not in MEDIATIDY_SIGN_P12"; exit 1; }
print "MEDIATIDY_SIGN_IDENTITY=$identity"
print "MEDIATIDY_SIGN_KEYCHAIN=$keychain"
