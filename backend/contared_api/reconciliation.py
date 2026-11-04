from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations
from typing import Literal


Side = Literal["bank", "books"]
Rule = Literal[
    "reference_amount",
    "amount_date",
    "reference_date_group",
    "reference_amount_mismatch",
    "ambiguous",
    "unmatched",
]
MAX_GROUP_SIZE = 3
MAX_GROUP_CANDIDATES = 2500
MAX_PAIR_COMPARISONS = 25000


@dataclass(frozen=True)
class Movement:
    id: str
    account_id: str
    side: Side
    transaction_date: date
    amount: Decimal
    reference: str | None = None


@dataclass(frozen=True)
class Proposal:
    rule: Rule
    bank_ids: tuple[str, ...]
    book_ids: tuple[str, ...]
    explanation: str


def propose_reconciliation(
    bank_movements: list[Movement],
    book_movements: list[Movement],
) -> list[Proposal]:
    movements = bank_movements + book_movements
    if any(movement.side != "bank" for movement in bank_movements):
        raise ValueError("bank_movements solo puede contener movimientos bancarios.")
    if any(movement.side != "books" for movement in book_movements):
        raise ValueError("book_movements solo puede contener movimientos contables.")
    if len({movement.id for movement in movements}) != len(movements):
        raise ValueError("Los identificadores de movimientos deben ser únicos.")
    if len({movement.account_id for movement in movements}) > 1:
        raise ValueError("Solo se pueden comparar movimientos de la misma cuenta.")
    if any(
        not isinstance(movement.amount, Decimal) or not movement.amount.is_finite()
        for movement in movements
    ):
        raise ValueError("Los montos deben ser valores Decimal finitos.")

    proposals: list[Proposal] = []
    claimed_bank: set[str] = set()
    claimed_books: set[str] = set()

    def consume_pairs(rule: Rule, predicate, explanation, candidate_key) -> None:
        available_bank = [item for item in bank_movements if item.id not in claimed_bank]
        available_books = [item for item in book_movements if item.id not in claimed_books]
        for banks, books in _pair_components(
            available_bank, available_books, predicate, candidate_key
        ):
            claimed_bank.update(item.id for item in banks)
            claimed_books.update(item.id for item in books)
            if len(banks) == len(books) == 1:
                bank, book = banks[0], books[0]
                proposals.append(
                    Proposal(rule, (bank.id,), (book.id,), explanation(bank, book))
                )
            else:
                proposals.append(
                    Proposal(
                        "ambiguous",
                        tuple(item.id for item in banks),
                        tuple(item.id for item in books),
                        "Hay varios candidatos compatibles; requiere revisión manual.",
                    )
                )

    consume_pairs(
        "reference_amount",
        lambda bank, book: bool(bank.reference)
        and bank.reference == book.reference
        and bank.amount == book.amount,
        lambda bank, book: (
            "Referencia y monto exactos. "
            f"Revisar fechas: banco {bank.transaction_date.isoformat()}, "
            f"libros {book.transaction_date.isoformat()}."
        ),
        lambda movement: (movement.reference, movement.amount)
        if movement.reference
        else None,
    )
    consume_pairs(
        "amount_date",
        lambda bank, book: bank.amount == book.amount
        and bank.transaction_date == book.transaction_date,
        lambda bank, book: (
            "Fecha y monto exactos; la referencia difiere o no está informada "
            f"(banco {bank.transaction_date.isoformat()}, "
            f"libros {book.transaction_date.isoformat()}). "
            "El candidato es único en ambos lados."
        ),
        lambda movement: (movement.transaction_date, movement.amount),
    )

    remaining_bank = [item for item in bank_movements if item.id not in claimed_bank]
    remaining_books = [item for item in book_movements if item.id not in claimed_books]
    for (bank_ids, book_ids), ambiguous in _group_components(
        remaining_bank, remaining_books
    ):
        claimed_bank.update(bank_ids)
        claimed_books.update(book_ids)
        proposals.append(
            Proposal(
                "ambiguous" if ambiguous else "reference_date_group",
                bank_ids,
                book_ids,
                (
                    "Hay varias agrupaciones posibles por referencia, fecha y monto; "
                    "requiere revisión manual."
                    if ambiguous
                    else f"Referencia y fecha exactas; agrupación limitada a {MAX_GROUP_SIZE} partidas."
                ),
            )
        )

    consume_pairs(
        "reference_amount_mismatch",
        lambda bank, book: bool(bank.reference)
        and bank.reference == book.reference
        and bank.amount != book.amount,
        lambda bank, book: (
            "La referencia coincide, pero los montos son distintos "
            f"(banco {bank.transaction_date.isoformat()}, "
            f"libros {book.transaction_date.isoformat()}). "
            "Es una diferencia, no una coincidencia confirmable."
        ),
        lambda movement: movement.reference if movement.reference else None,
    )

    for movement in bank_movements:
        if movement.id not in claimed_bank:
            proposals.append(
                Proposal("unmatched", (movement.id,), (), "Sin contraparte identificada.")
            )
    for movement in book_movements:
        if movement.id not in claimed_books:
            proposals.append(
                Proposal("unmatched", (), (movement.id,), "Sin contraparte identificada.")
            )
    return proposals


def _pair_components(bank_movements, book_movements, predicate, candidate_key):
    bank_groups = defaultdict(list)
    book_groups = defaultdict(list)
    for movement in bank_movements:
        key = candidate_key(movement)
        if key is not None:
            bank_groups[key].append(movement)
    for movement in book_movements:
        key = candidate_key(movement)
        if key is not None:
            book_groups[key].append(movement)

    components = []
    comparisons = 0
    for key in bank_groups.keys() & book_groups.keys():
        banks, books = bank_groups[key], book_groups[key]
        comparisons += len(banks) * len(books)
        if comparisons > MAX_PAIR_COMPARISONS:
            components.append((banks, books))
            continue
        graph: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for bank in banks:
            for book in books:
                if predicate(bank, book):
                    bank_key, book_key = ("bank", bank.id), ("books", book.id)
                    graph[bank_key].add(book_key)
                    graph[book_key].add(bank_key)
        visited: set[tuple[str, str]] = set()
        for node in graph:
            if node in visited:
                continue
            pending = [node]
            visited.add(node)
            bank_ids, book_ids = set(), set()
            while pending:
                current = pending.pop()
                (bank_ids if current[0] == "bank" else book_ids).add(current[1])
                for neighbor in graph[current]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        pending.append(neighbor)
            components.append(
                (
                    [item for item in banks if item.id in bank_ids],
                    [item for item in books if item.id in book_ids],
                )
            )
    return components


def _group_components(bank_movements, book_movements):
    by_reference_date_bank = defaultdict(list)
    by_reference_date_books = defaultdict(list)
    for movement in bank_movements:
        if movement.reference:
            by_reference_date_bank[(movement.reference, movement.transaction_date)].append(movement)
    for movement in book_movements:
        if movement.reference:
            by_reference_date_books[(movement.reference, movement.transaction_date)].append(movement)

    result = []
    for key in by_reference_date_bank.keys() & by_reference_date_books.keys():
        banks = by_reference_date_bank[key]
        books = by_reference_date_books[key]
        candidates = []
        search_exceeded = False
        combinations_examined = 0
        for singletons, grouped, bank_is_singleton in (
            (banks, books, True),
            (books, banks, False),
        ):
            for singleton in singletons:
                for size in range(2, min(MAX_GROUP_SIZE, len(grouped)) + 1):
                    for group in combinations(grouped, size):
                        combinations_examined += 1
                        if combinations_examined > MAX_GROUP_CANDIDATES:
                            search_exceeded = True
                            break
                        if sum((item.amount for item in group), Decimal("0")) != singleton.amount:
                            continue
                        group_ids = tuple(sorted(item.id for item in group))
                        candidates.append(
                            ((singleton.id,), group_ids)
                            if bank_is_singleton
                            else (group_ids, (singleton.id,))
                        )
                    if search_exceeded:
                        break
                if search_exceeded:
                    break
            if search_exceeded:
                break

        if search_exceeded:
            result.append(
                (
                    (tuple(sorted(item.id for item in banks)),
                     tuple(sorted(item.id for item in books))),
                    True,
                )
            )
            continue
        if not candidates:
            continue
        participants = defaultdict(set)
        for index, (bank_ids, book_ids) in enumerate(candidates):
            for movement_id in bank_ids:
                participants[("bank", movement_id)].add(index)
            for movement_id in book_ids:
                participants[("books", movement_id)].add(index)
        candidate_graph = defaultdict(set)
        for indexes in participants.values():
            for index in indexes:
                candidate_graph[index].update(indexes - {index})
        visited = set()
        for index in range(len(candidates)):
            if index in visited:
                continue
            pending = [index]
            component = set()
            while pending:
                current = pending.pop()
                if current in component:
                    continue
                component.add(current)
                pending.extend(candidate_graph[current] - component)
            visited.update(component)
            bank_ids = tuple(sorted({
                movement_id
                for candidate_index in component
                for movement_id in candidates[candidate_index][0]
            }))
            book_ids = tuple(sorted({
                movement_id
                for candidate_index in component
                for movement_id in candidates[candidate_index][1]
            }))
            result.append(((bank_ids, book_ids), len(component) > 1))
    return result
