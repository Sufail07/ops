from __future__ import annotations

from typing import Protocol

from core.alerts.format import Alert


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...

    def send_proposals(self, meeting_id: str, title: str, rows: list, goal_names: dict[str, str]) -> None: ...


class ConsoleNotifier:
    def send(self, alert: Alert) -> None:
        print(f"\n--- ALERT to {alert.owner or 'team lead'} [{alert.severity}] ---\n{alert.text}", flush=True)


    def send_proposals(self, meeting_id: str, title: str, rows: list, goal_names: dict[str, str]) -> None:
        print(f"\n--- {len(rows)} PROPOSAL(S) from '{title}' ({meeting_id}) ---", flush=True)


class MemoryNotifier:
    def __init__(self) -> None:
        self.sent: list[Alert] = []
        self.proposal_cards: list[tuple[str, str, list]] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)

    def send_proposals(self, meeting_id: str, title: str, rows: list, goal_names: dict[str, str]) -> None:
        self.proposal_cards.append((meeting_id, title, rows))
