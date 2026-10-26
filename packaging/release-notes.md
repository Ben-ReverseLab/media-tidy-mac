
## Install

1. Download **MediaTidy-VERSION.dmg** below, open it and drag **MediaTidy** into **Applications**.
2. **First start:** MediaTidy is not notarized by Apple, so macOS blocks it the first time.
   - macOS 15 or later: open MediaTidy, click **Done**, then go to **System Settings → Privacy & Security**,
     scroll down and click **Open Anyway** (within about an hour), confirm with your password.
   - macOS 13–14: right-click (Control-click) MediaTidy → **Open** → **Open**.
   - Alternatively in Terminal: `xattr -dr com.apple.quarantine /Applications/MediaTidy.app`
3. For the Apple Photos library: **System Settings → Privacy & Security → Full Disk Access** → add MediaTidy.
   Updates keep this permission.

Homebrew: `brew install --cask ben-reverselab/tap/mediatidy`

Verify the download: `shasum -a 256 -c MediaTidy-VERSION.dmg.sha256` and
`gh attestation verify MediaTidy-VERSION.dmg --repo Ben-ReverseLab/media-tidy-mac`
