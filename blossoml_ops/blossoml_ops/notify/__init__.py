"""Nostr notifications (NIP-17 private direct messages).

The recipient and the relays are fixed here on purpose: Claude cannot choose
who receives a message or where it is published.
"""

RECIPIENT_NPUB = "npub1svne455wa3rctcsnnhzjn2t9pldmgfpkd4rytewzsf8he02fysxsvgwhj4"
RECIPIENT_HEX = "83279ad28eec4785e2139dc529a9650fdbb424366d4645e5c2824f7cbd49240d"
RELAYS = (
    "wss://relay.primal.net",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://nostr.mom",
)
