"""Decisions ``makemigrations`` cannot make on its own.

The default :class:`Questioner` never guesses: renames are treated as
remove + add, and a NOT NULL column without a default needs a value from
``defaults={"Model.field": value}``.  :class:`InteractiveQuestioner`
asks on the terminal instead.
"""
from __future__ import annotations

import ast

from ..exceptions import MigrationError
from .backends.base import NOT_PROVIDED


class Questioner:
    def __init__(self, defaults: dict | None = None, renames: dict | None = None):
        # defaults: {"Model.field": value}; renames: {"Model.new_field": "old_field", "NewModel": "OldModel"}
        self.defaults = dict(defaults or {})
        self.renames = dict(renames or {})

    def ask_rename_model(self, old_name: str, new_name: str) -> bool:
        return self.renames.get(new_name) == old_name

    def ask_rename_field(self, model: str, old_name: str, new_name: str) -> bool:
        return self.renames.get(f"{model}.{new_name}") == old_name

    def ask_not_null_addition(self, model: str, field: str):
        key = f"{model}.{field}"
        if key in self.defaults:
            return self.defaults[key]
        raise MigrationError(
            f"{key} is NOT NULL without a default, and {model} may already have rows. "
            f"Give it a default=, make it nullable=True, or pass defaults={{'{key}': value}} "
            "(run interactively to be asked)."
        )

    def ask_not_null_alteration(self, model: str, field: str):
        """Value to replace existing NULLs with, or NOT_PROVIDED to leave them (fails if any)."""
        return self.defaults.get(f"{model}.{field}", NOT_PROVIDED)


class InteractiveQuestioner(Questioner):
    def __init__(self, *args, input_fn=input, output_fn=print, **kwargs):
        super().__init__(*args, **kwargs)
        self._input = input_fn
        self._print = output_fn

    def _yes(self, question: str) -> bool:
        while True:
            answer = self._input(f"{question} [y/N] ").strip().lower()
            if answer in ("", "n", "no"):
                return False
            if answer in ("y", "yes"):
                return True

    def _value(self, prompt: str):
        self._print("Enter a Python literal (e.g. 0, '', 'pending', None).")
        while True:
            raw = self._input(f"{prompt} >>> ").strip()
            try:
                return ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                self._print("Not a valid literal; try again.")

    def ask_rename_model(self, old_name, new_name):
        return super().ask_rename_model(old_name, new_name) or \
            self._yes(f"Was model {old_name} renamed to {new_name}?")

    def ask_rename_field(self, model, old_name, new_name):
        return super().ask_rename_field(model, old_name, new_name) or \
            self._yes(f"Was {model}.{old_name} renamed to {model}.{new_name}?")

    def ask_not_null_addition(self, model, field):
        key = f"{model}.{field}"
        if key in self.defaults:
            return self.defaults[key]
        self._print(f"{key} is NOT NULL without a default; existing rows need a value.")
        return self._value(f"Value for existing rows of {key}")

    def ask_not_null_alteration(self, model, field):
        key = f"{model}.{field}"
        if key in self.defaults:
            return self.defaults[key]
        self._print(f"{key} is becoming NOT NULL; existing NULLs must be replaced.")
        if not self._yes("Provide a value for existing NULL rows?"):
            return NOT_PROVIDED
        return self._value(f"Replacement for NULLs in {key}")
