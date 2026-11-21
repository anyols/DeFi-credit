"""M5: oracle read + classification -- the key feature of the whole risk engine, per SPEC.md.

Verified against morpho-org/morpho-blue-oracles MorphoChainlinkOracleV2.sol: feeds are
BASE_FEED_1/2, QUOTE_FEED_1/2 (Chainlink AggregatorV3Interface, zero address if unused), vault
conversions are BASE_VAULT/QUOTE_VAULT (ERC4626, zero address if unused). Not every oracle on
Morpho is this exact contract, so every field read below is best-effort: a revert means "this
oracle doesn't expose that field", not a fatal error, and an oracle where none of the known
fields can be read at all is classified UNKNOWN for manual review.
"""

from . import chain

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_FEED_FIELDS = ("BASE_FEED_1", "BASE_FEED_2", "QUOTE_FEED_1", "QUOTE_FEED_2")
_VAULT_FIELDS = ("BASE_VAULT", "QUOTE_VAULT")


def _is_set(address):
    return address is not None and address.lower() != ZERO_ADDRESS


def _try_call(address, fn_name, block):
    try:
        (value,) = chain.call(address, fn_name, [], block)
        return value
    except Exception:
        return None


def read_fields(oracle_address, block):
    return {fn: _try_call(oracle_address, fn, block) for fn in _FEED_FIELDS + _VAULT_FIELDS}


def feed_description(feed_address, block):
    if not _is_set(feed_address):
        return None
    try:
        (desc,) = chain.call(feed_address, "description", [], block)
        return desc
    except Exception:
        return None


def classify(oracle_address, block):
    """Returns {"class": one of MARKET/EXCHANGE_RATE/FIXED/UNKNOWN, "fields": {...}, "feeds": {...}}.

    MARKET: at least one feed tracks a traded market price.
    EXCHANGE_RATE: price comes only from a vault conversion rate -- a secondary-market depeg of
                   the underlying is invisible to this oracle.
    FIXED: no feeds, no vault -- the price never moves at all; every real price move is invisible.
    UNKNOWN: none of the known fields could be read -- a non-standard contract, flag for review.
    """
    fields = read_fields(oracle_address, block)
    if all(v is None for v in fields.values()):
        return {"class": "UNKNOWN", "fields": fields, "feeds": {}}

    feeds = {k: fields[k] for k in _FEED_FIELDS}
    has_feed = any(_is_set(v) for v in feeds.values())
    has_vault = any(_is_set(fields[k]) for k in _VAULT_FIELDS)

    if has_feed:
        cls = "MARKET"
    elif has_vault:
        cls = "EXCHANGE_RATE"
    else:
        cls = "FIXED"

    descriptions = {k: feed_description(v, block) for k, v in feeds.items() if _is_set(v)}
    return {"class": cls, "fields": fields, "feeds": descriptions}


def read_price(oracle_address, block):
    (price,) = chain.call(oracle_address, "price", [], block)
    return price
