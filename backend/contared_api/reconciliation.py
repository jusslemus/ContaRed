from collections import Counter, defaultdict
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
    matched_bank: set[str] = set()
    matched_books: set[str] = set()
    annotated_bank: set[str] = set()
    annotated_books: set[str] = set()
    reference_matches = _unique_pairs(
        bank_movements,
        book_movements,
        lambda bank, book: bool(bank.reference)
        and bank.reference == book.reference
        and bank.amount == book.amount,
    )
    for bank, book in reference_matches:
        proposals.append(
            Proposal(
                "reference_amount",
                (bank.id,),
                (book.id,),
                "Referencia y monto exactos.",
            )
        )
        matched_bank.add(bank.id)
        matched_books.add(book.id)

    for side_movements, opposite_movements, side_name in (
        (bank_movements, book_movements, "bank"),
        (book_movements, bank_movements, "books"),
    ):
        for movement in side_movements:
            if movement.id in (matched_bank if side_name == "bank" else matched_books):
                continue
            candidates = [
                other
                for other in opposite_movements
                if other.id
                not in (matched_books if side_name == "bank" else matched_bank)
                and other.amount == movement.amount
                and other.transaction_date == movement.transaction_date
            ]
            if len(candidates) > 1:
                _append_ambiguity(
                    proposals,
                    movement,
                    candidates,
                    side_name,
                    "Hay varios candidatos con el mismo monto y fecha.",
                )
                (annotated_bank if side_name == "bank" else annotated_books).add(
                    movement.id
                )
            elif len(candidates) == 1:
                other = candidates[0]
                reverse_candidates = [
                    candidate
                    for candidate in side_movements
                    if candidate.id
                    not in (
                        matched_bank if side_name == "bank" else matched_books
                    )
                    and candidate.amount == other.amount
                    and candidate.transaction_date == other.transaction_date
                ]
                if len(reverse_candidates) == 1:
                    bank, book = (
                        (movement, other) if side_name == "bank" else (other, movement)
                    )
                    proposals.append(
                        Proposal(
                            "amount_date",
                            (bank.id,),
                            (book.id,),
                            "Monto y fecha exactos; el candidato es único en ambos lados.",
                        )
                    )
                    matched_bank.add(bank.id)
                    matched_books.add(book.id)
                else:
                    _append_ambiguity(
                        proposals,
                        movement,
                        reverse_candidates,
                        side_name,
                        "El candidato comparte monto y fecha con varios movimientos.",
                    )
                    (annotated_bank if side_name == "bank" else annotated_books).add(
                        movement.id
                    )

    remaining_bank = [
        movement
        for movement in bank_movements
        if movement.id not in matched_bank and movement.id not in annotated_bank
    ]
    remaining_books = [
        movement
        for movement in book_movements
        if movement.id not in matched_books and movement.id not in annotated_books
    ]
    group_candidates = _group_candidates(remaining_bank, remaining_books)
    group_frequency = Counter(
        (side, movement_id)
        for candidate in group_candidates
        for side, ids in (("bank", candidate[0]), ("books", candidate[1]))
        for movement_id in ids
    )
    for bank_ids, book_ids in group_candidates:
        participant_keys = [
            *(("bank", movement_id) for movement_id in bank_ids),
            *(("books", movement_id) for movement_id in book_ids),
        ]
        if all(group_frequency[key] == 1 for key in participant_keys):
            proposals.append(
                Proposal(
                    "reference_date_group",
                    bank_ids,
                    book_ids,
                    f"Referencia y fecha exactas; agrupación limitada a {MAX_GROUP_SIZE} partidas.",
                )
            )
            matched_bank.update(bank_ids)
            matched_books.update(book_ids)
        else:
            proposals.append(
                Proposal(
                    "ambiguous",
                    bank_ids,
                    book_ids,
                    "Hay varias agrupaciones posibles con referencia, fecha y monto coincidentes.",
                )
            )
            annotated_bank.update(bank_ids)
            annotated_books.update(book_ids)

    remaining_bank_by_reference = _by_reference(
        [
            movement
            for movement in bank_movements
            if movement.id not in matched_bank and movement.id not in annotated_bank
        ]
    )
    remaining_books_by_reference = _by_reference(
        [
            movement
            for movement in book_movements
            if movement.id not in matched_books and movement.id not in annotated_books
        ]
    )
    for reference in remaining_bank_by_reference.keys() & remaining_books_by_reference.keys():
        for bank in remaining_bank_by_reference[reference]:
            for book in remaining_books_by_reference[reference]:
                if book.amount != bank.amount:
                    proposals.append(
                        Proposal(
                            "reference_amount_mismatch",
                            (bank.id,),
                            (book.id,),
                            "La referencia coincide, pero los montos son distintos.",
                        )
                    )
                    annotated_bank.add(bank.id)
                    annotated_books.add(book.id)

    for movement in bank_movements:
        if movement.id not in matched_bank and movement.id not in annotated_bank:
            proposals.append(
                Proposal("unmatched", (movement.id,), (), "Sin contraparte identificada.")
            )
    for movement in book_movements:
        if movement.id not in matched_books and movement.id not in annotated_books:
            proposals.append(
                Proposal("unmatched", (), (movement.id,), "Sin contraparte identificada.")
            )

    return proposals


def _by_reference(movements: list[Movement]) -> dict[str, list[Movement]]:
    grouped: dict[str, list[Movement]] = defaultdict(list)
    for movement in movements:
        if movement.reference:
            grouped[movement.reference].append(movement)
    return grouped


def _unique_pairs(bank_movements, book_movements, predicate):
    pairs = []
    for bank in bank_movements:
        candidates = [book for book in book_movements if predicate(bank, book)]
        if len(candidates) != 1:
            continue
        book = candidates[0]
        reverse = [candidate for candidate in bank_movements if predicate(candidate, book)]
        if len(reverse) == 1:
            pairs.append((bank, book))
    return pairs


def _append_ambiguity(proposals, movement, candidates, side, explanation):
    bank_ids = (movement.id,) if side == "bank" else tuple(
        candidate.id for candidate in candidates
    )
    book_ids = tuple(candidate.id for candidate in candidates) if side == "bank" else (
        movement.id,
    )
    proposals.append(Proposal("ambiguous", bank_ids, book_ids, explanation))


def _group_candidates(bank_movements, book_movements):
    candidates = []
    for bank_groups, book_groups, bank_is_group in (
        (bank_movements, book_movements, False),
        (book_movements, bank_movements, True),
    ):
        for singleton in bank_groups:
            if not singleton.reference:
                continue
            eligible = [
                movement
                for movement in book_groups
                if movement.reference == singleton.reference
                and movement.transaction_date == singleton.transaction_date
            ]
            for size in range(2, min(MAX_GROUP_SIZE, len(eligible)) + 1):
                for group in combinations(eligible, size):
                    if sum((movement.amount for movement in group), Decimal("0")) != singleton.amount:
                        continue
                    bank_ids, book_ids = (
                        (tuple(item.id for item in group), (singleton.id,))
                        if bank_is_group
                        else ((singleton.id,), tuple(item.id for item in group))
                    )
                    candidates.append((bank_ids, book_ids))
    return candidates
