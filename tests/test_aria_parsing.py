"""Unit tests for the aria_snapshot(mode="ai") parser -- no browser needed. Fixture text is
real captured output from MockBank pages, not invented.
"""
from __future__ import annotations

from surface.aria import parse_aria_snapshot

LOGIN_SNAPSHOT = """
- table [ref=e2]:
  - rowgroup [ref=e3]:
    - row [ref=e4]:
      - cell [ref=e5]:
        - table [ref=e6]:
          - rowgroup [ref=e7]:
            - row [ref=e8]:
              - cell "MockBank Core v3.2" [ref=e9]
              - cell [ref=e10]
    - row [ref=e11]:
      - cell [ref=e12]:
        - table [ref=e13]:
          - rowgroup [ref=e14]:
            - row [ref=e15]:
              - cell [ref=e16]:
                - table [ref=e18]:
                  - rowgroup [ref=e19]:
                    - row [ref=e20]:
                      - cell [ref=e21]:
                        - heading "Operator Log In" [level=1] [ref=e22]
                    - row [ref=e23]:
                      - cell "Username" [ref=e24]
                      - cell [ref=e25]:
                        - textbox "Username" [ref=e26]
                    - row [ref=e27]:
                      - cell "Password" [ref=e28]
                      - cell [ref=e29]:
                        - textbox "Password" [ref=e30]
                    - row [ref=e31]:
                      - cell [ref=e32]:
                        - button "Log In" [ref=e33]
"""

SELECT_SNAPSHOT = """
- table [ref=f3e21]:
  - rowgroup [ref=f3e22]:
    - row [ref=f3e23]:
      - cell "Account Type" [ref=f3e24]
      - cell "Savings" [ref=f3e25]:
        - combobox "Account Type" [ref=f3e26]:
          - option "Savings" [selected]
          - option "Checking"
    - row [ref=f3e27]:
      - cell "Initial Deposit Amount" [ref=f3e28]
      - cell [ref=f3e29]:
        - textbox "Initial Deposit Amount" [ref=f3e30]
"""

DIALOG_SNAPSHOT = """
- generic [active] [ref=f5e1]:
  - table [ref=f5e2]:
    - rowgroup [ref=f5e3]:
      - row [ref=f5e4]:
        - cell [ref=f5e5]:
          - table [ref=f5e6]:
            - rowgroup [ref=f5e7]:
              - row [ref=f5e8]:
                - cell "MockBank Core v3.2" [ref=f5e9]
      - row [ref=f5e13]:
        - cell [ref=f5e14]:
          - table [ref=f5e20]:
            - rowgroup [ref=f5e21]:
              - row [ref=f5e25]:
                - cell "Member ID" [ref=f5e26]
                - cell [ref=f5e27]:
                  - textbox "Member ID" [ref=f5e28]
              - row [ref=f5e29]:
                - cell [ref=f5e30]:
                  - button "Search" [ref=f5e31]
  - dialog "Terms Updated" [ref=f5e32]:
    - generic [ref=f5e33]:
      - heading "Terms Updated" [level=3] [ref=f5e34]
      - paragraph [ref=f5e35]: Our terms of service have been updated. Please review and dismiss to continue.
      - button "Dismiss" [ref=f5e37]
"""

VALUE_SNAPSHOT = """
- textbox "Username" [active] [ref=e26]: operator
"""


def test_drops_structural_wrappers_keeps_named_and_interactive():
    elements = parse_aria_snapshot(LOGIN_SNAPSHOT)
    roles_names = {(el.role, el.name) for el in elements}
    assert ("table", None) not in roles_names
    assert ("rowgroup", None) not in roles_names
    assert ("row", None) not in roles_names
    assert ("cell", None) not in roles_names  # unnamed cell (e10) dropped
    assert ("cell", "MockBank Core v3.2") in roles_names  # named cell kept
    assert ("textbox", "Username") in roles_names
    assert ("textbox", "Password") in roles_names
    assert ("button", "Log In") in roles_names
    assert ("heading", "Operator Log In") in roles_names


def test_combobox_rolls_up_options_not_listed_separately():
    elements = parse_aria_snapshot(SELECT_SNAPSHOT)
    comboboxes = [el for el in elements if el.role == "combobox"]
    assert len(comboboxes) == 1
    combo = comboboxes[0]
    assert combo.name == "Account Type"
    assert combo.value == "Savings"
    assert combo.options == ["Savings", "Checking"]
    assert not any(el.role == "option" for el in elements)


def test_dialog_scopes_out_background_elements():
    elements = parse_aria_snapshot(DIALOG_SNAPSHOT)
    roles_names = {(el.role, el.name) for el in elements}
    assert ("dialog", "Terms Updated") in roles_names
    assert ("button", "Dismiss") in roles_names
    assert ("button", "Search") not in roles_names
    assert ("textbox", "Member ID") not in roles_names


def test_parses_current_value_after_colon():
    elements = parse_aria_snapshot(VALUE_SNAPSHOT)
    assert elements[0].value == "operator"


def test_to_prompt_text_is_stable_and_readable():
    elements = parse_aria_snapshot(LOGIN_SNAPSHOT)
    from surface.base import ObservedState
    state = ObservedState(url="http://localhost:8000/login", title="Log In", elements=elements)
    text = state.to_prompt_text()
    assert "textbox \"Username\"" in text
    assert "button \"Log In\"" in text
