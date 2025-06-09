from pr_agent.tools.ticket_pr_compliance_check import find_trello_cards, find_trello_card_from_branch


def test_find_trello_cards_from_description():
    text = "See card https://trello.com/c/abc12345 for details"
    cards = find_trello_cards(text)
    assert "abc12345" in cards


def test_find_trello_card_from_branch():
    assert find_trello_card_from_branch("abc12345-feature") == "abc12345"
    assert find_trello_card_from_branch("feature/abc12345/fix") == "abc12345"
    assert find_trello_card_from_branch("feature/noid") is None
