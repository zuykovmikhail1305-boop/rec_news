"""Create and verify evidence against the exact stored article text."""

from hashlib import sha256

from models import NewsQuote, NewsRaw


def create_quote(raw: NewsRaw, quote_text: str, start_char: int, end_char: int) -> NewsQuote:
    """Offsets are Python Unicode character indexes; end_char is exclusive.

    The caller adds the returned quote to its database transaction.
    """
    text = raw.text
    if text is None:
        raise ValueError("Publication has no extracted text")
    if not 0 <= start_char < end_char <= len(text):
        raise ValueError("Quote offsets are outside the publication text")
    if text[start_char:end_char] != quote_text:
        raise ValueError("Quote does not match the publication text")
    return NewsQuote(
        publication=raw,
        quote_text=quote_text,
        start_char=start_char,
        end_char=end_char,
        text_hash=sha256(text.encode("utf-8")).hexdigest(),
    )


def quote_matches_text(quote: NewsQuote, text: str) -> bool:
    """Recheck evidence after loading it or reparsing the publication."""
    return (
        0 <= quote.start_char < quote.end_char <= len(text)
        and quote.text_hash == sha256(text.encode("utf-8")).hexdigest()
        and text[quote.start_char:quote.end_char] == quote.quote_text
    )
