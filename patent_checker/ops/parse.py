"""Parsers for EPO OPS XML responses (pure functions, fixture-testable).

Response bodies come from :mod:`ops_client`; these functions never touch the
network. XML namespaces: default (exchange) ``http://www.epo.org/exchange``
and ``ops`` ``http://ops.epo.org``.
"""

from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from patent_checker.pubnum import PubNumber

_EXCHANGE_NS = "http://www.epo.org/exchange"
_OPS_NS = "http://ops.epo.org"


@dataclass(frozen=True)
class OpsSearchHit:
    """One search hit: publication (docdb spelling) and its family id."""

    pub: str
    family_id: str


@dataclass(frozen=True)
class OpsSearchPage:
    """One page of a published-data CQL search result."""

    total_count: int
    query: str
    begin: int
    end: int
    hits: tuple[OpsSearchHit, ...]


@dataclass(frozen=True)
class OpsBiblio:
    """Bibliographic data of one exchange document."""

    pub: str
    family_id: str
    title: str
    abstract: str
    applicants: tuple[str, ...]
    inventors: tuple[str, ...]
    ipc: tuple[str, ...]
    cpc: tuple[str, ...]
    publication_date: str
    cited_patents: tuple[str, ...]
    npl_citation_count: int


@dataclass(frozen=True)
class OpsSearchBiblioPage:
    """One page of a biblio-constituent search: full biblio per hit."""

    total_count: int
    begin: int
    end: int
    docs: tuple[OpsBiblio, ...]


@dataclass(frozen=True)
class OpsLegalEvent:
    """One INPADOC legal event."""

    code: str
    desc: str
    gazette_date: str
    pre_lines: tuple[str, ...]


@dataclass(frozen=True)
class OpsFamily:
    """Simple patent family: id and member publications (docdb spelling)."""

    family_id: str
    members: tuple[str, ...]


def parse_search_xml(xml: bytes) -> OpsSearchPage:
    """Parse a ``published-data/search`` response.

    - ``total_count``: ``ops:biblio-search@total-result-count``.
    - ``query``: text of ``ops:query``.
    - ``begin`` / ``end``: attributes of ``ops:range``.
    - ``hits``: one per ``ops:publication-reference`` (``family-id``
      attribute; publication from the ``document-id[document-id-type=docdb]``
      country / doc-number / kind, rendered in docdb spelling via
      :class:`pubnum.PubNumber`).

    Raises:
        ValueError: If the document is not a search response.
    """
    root = etree.fromstring(xml)
    biblio_search = root.find(_ops("biblio-search"))
    if biblio_search is None:
        raise ValueError("not a published-data search response: missing ops:biblio-search")

    total_count = int(biblio_search.get("total-result-count", "0"))

    query_elem = biblio_search.find(_ops("query"))
    query = (query_elem.text or "").strip() if query_elem is not None else ""

    range_elem = biblio_search.find(_ops("range"))
    begin = int(range_elem.get("begin", "0")) if range_elem is not None else 0
    end = int(range_elem.get("end", "0")) if range_elem is not None else 0

    search_result = biblio_search.find(_ops("search-result"))
    hit_elems = (
        search_result.findall(_ops("publication-reference")) if search_result is not None else []
    )

    hits = []
    for pub_ref in hit_elems:
        family_id = pub_ref.get("family-id", "")
        doc_id = _find_docdb_document_id(pub_ref, "search hit publication-reference")
        hits.append(OpsSearchHit(pub=_docdb_pub(doc_id), family_id=family_id))

    return OpsSearchPage(
        total_count=total_count,
        query=query,
        begin=begin,
        end=end,
        hits=tuple(hits),
    )


def parse_biblio_xml(xml: bytes) -> OpsBiblio:
    """Parse a ``published-data/.../biblio`` response (first exchange document).

    - ``pub`` / ``family_id``: from ``exchange-document`` attributes
      (country / doc-number / kind, docdb spelling).
    - ``title``: ``invention-title`` preferring ``lang="en"``.
    - ``abstract``: abstract text preferring ``lang="en"``, whitespace
      normalized ("" if absent).
    - ``applicants`` / ``inventors``: names preferring entries with
      ``data-format="original"``; order kept, duplicates removed.
    - ``ipc``: ``classification-ipcr`` text values, whitespace normalized.
    - ``cpc``: ``patent-classification`` entries joined as
      ``section + class + subclass + main-group + "/" + subgroup``
      (e.g. ``G06N3/10``), deduplicated, document order.
    - ``publication_date``: date of the docdb publication document-id
      ("" if absent).
    - ``cited_patents``: ``patcit`` docdb document-ids in docdb spelling.
    - ``npl_citation_count``: number of ``nplcit`` elements.

    Raises:
        ValueError: If no ``exchange-document`` is present.
    """
    root = etree.fromstring(xml)
    exchange_document = root.find(f".//{_ex('exchange-document')}")
    if exchange_document is None:
        raise ValueError("not a biblio response: missing exchange-document")
    return _parse_exchange_document(exchange_document)


def parse_search_biblio_xml(xml: bytes) -> OpsSearchBiblioPage:
    """Parse a ``published-data/search/biblio`` (biblio constituent) response.

    ``total_count`` / ``begin`` / ``end`` come from ``ops:biblio-search`` and
    ``ops:range`` as in :func:`parse_search_xml`; each hit is one
    ``exchange-document`` parsed like :func:`parse_biblio_xml`.

    Raises:
        ValueError: If the document is not a search response.
    """
    root = etree.fromstring(xml)
    biblio_search = root.find(_ops("biblio-search"))
    if biblio_search is None:
        raise ValueError("not a published-data search response: missing ops:biblio-search")

    total_count = int(biblio_search.get("total-result-count", "0"))
    range_elem = biblio_search.find(_ops("range"))
    begin = int(range_elem.get("begin", "0")) if range_elem is not None else 0
    end = int(range_elem.get("end", "0")) if range_elem is not None else 0

    docs = tuple(
        _parse_exchange_document(elem)
        for elem in biblio_search.findall(f".//{_ex('exchange-document')}")
    )
    return OpsSearchBiblioPage(total_count=total_count, begin=begin, end=end, docs=docs)


def _parse_exchange_document(exchange_document: etree._Element) -> OpsBiblio:
    """Parse one ``exchange-document`` element into an :class:`OpsBiblio`."""
    pub = PubNumber(
        country=exchange_document.get("country", ""),
        number=exchange_document.get("doc-number", ""),
        kind=exchange_document.get("kind", ""),
    ).docdb()
    family_id = exchange_document.get("family-id", "")

    bibliographic_data = exchange_document.find(_ex("bibliographic-data"))
    if bibliographic_data is None:
        raise ValueError("biblio response missing bibliographic-data")

    title = _biblio_title(bibliographic_data)
    abstract = _biblio_abstract(exchange_document)

    parties = bibliographic_data.find(_ex("parties"))
    applicants_elem = parties.find(_ex("applicants")) if parties is not None else None
    inventors_elem = parties.find(_ex("inventors")) if parties is not None else None
    applicants = _extract_names(applicants_elem, "applicant")
    inventors = _extract_names(inventors_elem, "inventor")

    ipc = _extract_ipc(bibliographic_data)
    cpc = _extract_cpc(bibliographic_data)
    publication_date = _biblio_publication_date(bibliographic_data)
    cited_patents, npl_citation_count = _extract_citations(bibliographic_data)

    return OpsBiblio(
        pub=pub,
        family_id=family_id,
        title=title,
        abstract=abstract,
        applicants=applicants,
        inventors=inventors,
        ipc=ipc,
        cpc=cpc,
        publication_date=publication_date,
        cited_patents=cited_patents,
        npl_citation_count=npl_citation_count,
    )


def parse_legal_xml(xml: bytes) -> tuple[OpsLegalEvent, ...]:
    """Parse a ``legal/publication`` response into its legal events.

    One event per ``ops:legal`` element: ``code`` / ``desc`` attributes
    (whitespace stripped), ``gazette_date`` from the child element whose
    ``desc`` attribute equals ``"Gazette DATE"`` ("" if absent), and the
    text of every ``ops:pre`` child in order.

    Raises:
        ValueError: If the document contains no legal section at all.
    """
    root = etree.fromstring(xml)
    legal_elems = root.findall(f".//{_ops('legal')}")
    if not legal_elems:
        raise ValueError("not a legal response: no ops:legal elements present")

    events = []
    for legal in legal_elems:
        code = (legal.get("code") or "").strip()
        desc = (legal.get("desc") or "").strip()
        gazette_date = ""
        pre_lines = []
        for child in legal:
            if child.tag == _ops("pre"):
                pre_lines.append(child.text or "")
            elif not gazette_date and child.get("desc") == "Gazette DATE":
                gazette_date = (child.text or "").strip()
        events.append(
            OpsLegalEvent(
                code=code,
                desc=desc,
                gazette_date=gazette_date,
                pre_lines=tuple(pre_lines),
            )
        )

    return tuple(events)


def parse_family_xml(xml: bytes) -> OpsFamily:
    """Parse a ``family/publication`` response.

    - ``family_id``: ``family-id`` attribute of the first
      ``ops:family-member``.
    - ``members``: for each ``ops:family-member``, the publication-reference
      ``document-id[document-id-type=docdb]`` in docdb spelling; order kept,
      duplicates removed.

    Raises:
        ValueError: If no ``ops:family-member`` is present.
    """
    root = etree.fromstring(xml)
    member_elems = root.findall(f".//{_ops('family-member')}")
    if not member_elems:
        raise ValueError("not a family response: no ops:family-member elements present")

    family_id = member_elems[0].get("family-id", "")

    seen: set[str] = set()
    members: list[str] = []
    for member in member_elems:
        pub_ref = member.find(_ex("publication-reference"))
        if pub_ref is None:
            raise ValueError("family-member missing publication-reference")
        doc_id = _find_docdb_document_id(pub_ref, "family-member publication-reference")
        pub = _docdb_pub(doc_id)
        if pub not in seen:
            seen.add(pub)
            members.append(pub)

    return OpsFamily(family_id=family_id, members=tuple(members))


# --- Private helpers ---------------------------------------------------


def _ex(tag: str) -> str:
    """Build a Clark-notation qualified tag name in the exchange namespace."""
    return f"{{{_EXCHANGE_NS}}}{tag}"


def _ops(tag: str) -> str:
    """Build a Clark-notation qualified tag name in the ops namespace."""
    return f"{{{_OPS_NS}}}{tag}"


def _find_docdb_document_id(container: etree._Element, context: str) -> etree._Element:
    """Return the docdb-flavoured ``document-id`` child of *container*.

    Raises:
        ValueError: If no such child is present.
    """
    doc_id = container.find(f'{_ex("document-id")}[@document-id-type="docdb"]')
    if doc_id is None:
        raise ValueError(f"missing docdb document-id in {context}")
    return doc_id


def _docdb_pub(document_id: etree._Element) -> str:
    """Render a docdb ``document-id`` element as a docdb-spelled publication number."""
    country = (document_id.findtext(_ex("country")) or "").strip()
    number = (document_id.findtext(_ex("doc-number")) or "").strip()
    kind = (document_id.findtext(_ex("kind")) or "").strip()
    return PubNumber(country=country, number=number, kind=kind).docdb()


def _normalize_ws(text: str) -> str:
    """Collapse runs of whitespace into single spaces and strip the ends."""
    return " ".join(text.split())


def _clean_name(text: str) -> str:
    """Normalize a person name: strip surrounding whitespace and a trailing comma."""
    return text.strip().rstrip(", ").strip()


def _extract_names(parent: etree._Element | None, item_tag: str) -> tuple[str, ...]:
    """Extract applicant/inventor names, preferring ``data-format="original"`` entries.

    Names carrying ``data-format="original"`` are used whenever at least one
    is present; other formats are used only as a fallback. The result keeps
    document order and removes exact duplicates.
    """
    if parent is None:
        return ()

    name_tag = f"{item_tag}-name"
    original: list[str] = []
    fallback: list[str] = []
    for item in parent.findall(_ex(item_tag)):
        raw = item.findtext(f"{_ex(name_tag)}/{_ex('name')}")
        if raw is None:
            continue
        name = _clean_name(raw)
        if not name:
            continue
        if item.get("data-format") == "original":
            original.append(name)
        else:
            fallback.append(name)

    names = original if original else fallback

    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _biblio_title(bibliographic_data: etree._Element) -> str:
    """Return the ``invention-title``, preferring the English one."""
    titles = bibliographic_data.findall(_ex("invention-title"))
    for title in titles:
        if title.get("lang") == "en":
            return (title.text or "").strip()
    if titles:
        return (titles[0].text or "").strip()
    return ""


def _biblio_abstract(exchange_document: etree._Element) -> str:
    """Return the abstract text, preferring the English one, whitespace normalized."""
    abstracts = exchange_document.findall(_ex("abstract"))
    chosen = None
    for abstract in abstracts:
        if abstract.get("lang") == "en":
            chosen = abstract
            break
    if chosen is None and abstracts:
        chosen = abstracts[0]
    if chosen is None:
        return ""
    return _normalize_ws("".join(chosen.itertext()))


def _extract_ipc(bibliographic_data: etree._Element) -> tuple[str, ...]:
    """Extract whitespace-normalized IPC classification texts, in document order."""
    texts = []
    for text_elem in bibliographic_data.findall(f".//{_ex('classification-ipcr')}/{_ex('text')}"):
        text = _normalize_ws(text_elem.text or "")
        if text:
            texts.append(text)
    return tuple(texts)


def _extract_cpc(bibliographic_data: etree._Element) -> tuple[str, ...]:
    """Extract CPC symbols from ``patent-classification`` entries, deduplicated in order."""
    seen: set[str] = set()
    result: list[str] = []
    for classification in bibliographic_data.findall(f".//{_ex('patent-classification')}"):
        section = (classification.findtext(_ex("section")) or "").strip()
        class_ = (classification.findtext(_ex("class")) or "").strip()
        subclass = (classification.findtext(_ex("subclass")) or "").strip()
        main_group = (classification.findtext(_ex("main-group")) or "").strip()
        subgroup = (classification.findtext(_ex("subgroup")) or "").strip()
        symbol = f"{section}{class_}{subclass}{main_group}/{subgroup}"
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return tuple(result)


def _biblio_publication_date(bibliographic_data: etree._Element) -> str:
    """Return the date of the docdb publication ``document-id`` ("" if absent)."""
    pub_ref = bibliographic_data.find(_ex("publication-reference"))
    if pub_ref is None:
        return ""
    doc_id = pub_ref.find(f'{_ex("document-id")}[@document-id-type="docdb"]')
    if doc_id is None:
        return ""
    return (doc_id.findtext(_ex("date")) or "").strip()


def _extract_citations(bibliographic_data: etree._Element) -> tuple[tuple[str, ...], int]:
    """Return docdb-spelled ``patcit`` citations and the ``nplcit`` count."""
    references = bibliographic_data.find(_ex("references-cited"))
    if references is None:
        return (), 0

    cited_patents = []
    for patcit in references.findall(f".//{_ex('patcit')}"):
        doc_id = _find_docdb_document_id(patcit, "patcit")
        cited_patents.append(_docdb_pub(doc_id))

    npl_citation_count = len(references.findall(f".//{_ex('nplcit')}"))
    return tuple(cited_patents), npl_citation_count
