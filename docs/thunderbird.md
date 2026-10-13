# Thunderbird

Use **Manual config** when adding the account: automatic Mail.ru discovery points at the
official paid IMAP/SMTP service rather than your bridge.

| Setting | IMAP | SMTP |
| --- | --- | --- |
| Host | Your bridge host | Your bridge host |
| Port | Configured IMAP port | Configured SMTP port |
| Connection security | SSL/TLS for implicit TLS | SSL/TLS for implicit TLS |
| Authentication | Normal password | Normal password |
| Username | Full Mail.ru address | Full Mail.ru address |
| Password | Bridge password | Bridge password |

Reach the bridge over the home network or VPN. If Thunderbird does not already trust your
home CA, add it in Thunderbird's certificate settings. Defaults in `setup` are 1143/1025;
your deployment can use 993/465. Select **SSL/TLS**, rather than STARTTLS, for a TLS-enabled bridge.

## Copies, drafts, and folders

- In **Account Settings → Copies & Folders**, clear **Place a copy in** under sent messages.
  Mail.ru saves Sent automatically. Extra Sent APPEND is refused; a client warning after an
  SMTP success does not mean delivery failed, so do not resend solely because of that warning.
- Select the bridge's existing **Drafts** mailbox for drafts. UIDPLUS returns the newly saved
  draft UID directly, so Thunderbird can replace its previous autosaved revision without
  relying on the original Message-ID surviving Mail.ru's structured composer.
- Keep **Templates** in **Local Folders**. The API supports draft creation, but cannot import
  arbitrary RFC822 messages into other mailboxes.
- Choose an existing bridge folder for archives, or use Local Folders. Same-account moves
  use IMAP MOVE. IMAP COPY, importing from another account, and moving Local Folder messages
  into the bridge are unsupported. Downloading/copying bridge messages to Local Folders works.
- Thunderbird discovers Sent/Drafts/Trash/Spam/Archive from the folder attributes. Use the
  subscription dialog if a folder is hidden; subscription choices persist in the bridge.

Tags, junk-classification keywords, and client reply/forward indicators persist locally in
the bridge cache, including across restarts and bridge moves. All bridge clients see them;
they are not written to Mail.ru's web interface by the marks endpoint. Read/unread and star
changes are written through normally. A SMTP reply uses Mail.ru's `source.reply` reference
when its In-Reply-To Message-ID uniquely matches downloaded headers in the bridge cache;
it does not guess based on subject or download history to find an uncached original.

Mail.ru reconstructs drafts and outgoing messages from structured fields. Arbitrary custom
headers, Thunderbird-specific draft options, and original Message-IDs are not guaranteed
to survive a later API refresh. Choose **Local Folders** for drafts when retaining those
exact options matters. Signed/encrypted PGP/MIME and S/MIME submission is rejected with a
permanent SMTP error before any API send; the bridge cannot preserve those formats through
JSON composition. Receiving original signed/encrypted messages is unaffected.

## History and offline downloads

The bridge exposes the newest `initial_depth` messages per folder on first selection (default
2000), plus later arrivals. Thunderbird cannot fetch older mail that the bridge has not cached.
Increasing this setting does not automatically backfill folders already marked deeply synced;
do not delete the cache to work around it, since that would reset IMAP identity.

Thunderbird's **Synchronization & Storage** settings control downloading whole messages for
offline use. Disable account-wide offline downloads, or choose only needed folders, if you
want bodies and attachments loaded on demand. Thunderbird still downloads headers for its
message list; because the API provides full raw sources, those initial header requests can
also download/cache bodies. Full offline sync of a large folder can take time. Partial body
FETCH works against the cached source and returns the corrected RFC822.SIZE.

## Verification

The compatibility review used Thunderbird's current
[IMAP protocol source](https://github.com/mozilla/releases-comm-central/blob/master/mailnews/imap/src/nsImapProtocol.cpp)
and [response parser](https://github.com/mozilla/releases-comm-central/blob/master/mailnews/imap/src/nsImapServerResponseParser.cpp),
plus the [UIDPLUS](https://www.rfc-editor.org/rfc/rfc4315.txt) and
[MOVE](https://www.rfc-editor.org/rfc/rfc6851.txt) specifications. No Mozilla code was copied.

Local socket replays cover namespace/subscription discovery, Thunderbird's header-fetch
attributes, chunked downloads (including past EOF), tags, UID-mapped moves, non-synchronizing
APPEND literals, repeated draft saves, and scoped deletion of the old draft. Mailbox mutations
use mocks. This is protocol testing; the Thunderbird GUI was not installed on the test host.
