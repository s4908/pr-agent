import re
import traceback
from typing import List, Optional
import requests

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import GithubProvider
from pr_agent.log import get_logger

# Compile the regex pattern once, outside the function
GITHUB_TICKET_PATTERN = re.compile(
     r'(https://github[^/]+/[^/]+/[^/]+/issues/\d+)|(\b(\w+)/(\w+)#(\d+)\b)|(#\d+)'
)

# Regex to identify Trello card links like https://trello.com/c/<CARD_ID>
TRELLO_CARD_PATTERN = re.compile(r'https?://trello\.com/c/([A-Za-z0-9]+)')


def find_trello_cards(text: str) -> List[str]:
    """Extract Trello card short IDs from the given text."""
    if not text:
        return []
    return list({match for match in TRELLO_CARD_PATTERN.findall(text)})


def find_trello_card_from_branch(branch: str) -> Optional[str]:
    """Attempt to derive Trello card ID from branch name."""
    if not branch:
        return None
    for seg in re.split(r'[/-]', branch):
        if len(seg) == 8 and seg.isalnum():
            return seg
    return None


def fetch_trello_card(card_id: str) -> Optional[dict]:
    """Fetch Trello card details using the API if credentials are configured."""
    api_key = get_settings().get('trello.api_key', None)
    token = get_settings().get('trello.api_token', None)
    if not api_key or not token:
        get_logger().debug('Trello credentials missing, skipping fetch')
        return None
    url = f'https://api.trello.com/1/cards/{card_id}'
    try:
        resp = requests.get(url, params={'key': api_key, 'token': token}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return {
                'ticket_id': card_id,
                'ticket_url': f'https://trello.com/c/{card_id}',
                'title': data.get('name', ''),
                'body': data.get('desc', '')
            }
        get_logger().warning(f'Failed to fetch Trello card {card_id}: {resp.status_code}')
    except Exception as e:
        get_logger().error(f'Error fetching Trello card {card_id}: {e}')
    return None

def find_jira_tickets(text):
    # Regular expression patterns for JIRA tickets
    patterns = [
        r'\b[A-Z]{2,10}-\d{1,7}\b',  # Standard JIRA ticket format (e.g., PROJ-123)
        r'(?:https?://[^\s/]+/browse/)?([A-Z]{2,10}-\d{1,7})\b'  # JIRA URL or just the ticket
    ]

    tickets = set()
    for pattern in patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if isinstance(match, tuple):
                # If it's a tuple (from the URL pattern), take the last non-empty group
                ticket = next((m for m in reversed(match) if m), None)
            else:
                ticket = match
            if ticket:
                tickets.add(ticket)

    return list(tickets)


def extract_ticket_links_from_pr_description(pr_description, repo_path, base_url_html='https://github.com'):
    """
    Extract all ticket links from PR description
    """
    github_tickets = set()
    try:
        # Use the updated pattern to find matches
        matches = GITHUB_TICKET_PATTERN.findall(pr_description)

        for match in matches:
            if match[0]:  # Full URL match
                github_tickets.add(match[0])
            elif match[1]:  # Shorthand notation match: owner/repo#issue_number
                owner, repo, issue_number = match[2], match[3], match[4]
                github_tickets.add(f'{base_url_html.strip("/")}/{owner}/{repo}/issues/{issue_number}')
            else:  # #123 format
                issue_number = match[5][1:]  # remove #
                if issue_number.isdigit() and len(issue_number) < 5 and repo_path:
                    github_tickets.add(f'{base_url_html.strip("/")}/{repo_path}/issues/{issue_number}')

        if len(github_tickets) > 3:
            get_logger().info(f"Too many tickets found in PR description: {len(github_tickets)}")
            # Limit the number of tickets to 3
            github_tickets = set(list(github_tickets)[:3])
    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})

    return list(github_tickets)


async def extract_tickets(git_provider):
    MAX_TICKET_CHARACTERS = 10000
    try:
        if isinstance(git_provider, GithubProvider):
            user_description = git_provider.get_user_description()
            tickets = extract_ticket_links_from_pr_description(user_description, git_provider.repo, git_provider.base_url_html)
            trello_cards = find_trello_cards(user_description)
            branch_card = find_trello_card_from_branch(git_provider.get_pr_branch())
            if branch_card:
                trello_cards.append(branch_card)
            trello_cards = list(set(trello_cards))
            tickets_content = []

            if tickets:

                for ticket in tickets:
                    repo_name, original_issue_number = git_provider._parse_issue_url(ticket)

                    try:
                        issue_main = git_provider.repo_obj.get_issue(original_issue_number)
                    except Exception as e:
                        get_logger().error(f"Error getting main issue: {e}",
                                           artifact={"traceback": traceback.format_exc()})
                        continue

                    issue_body_str = issue_main.body or ""
                    if len(issue_body_str) > MAX_TICKET_CHARACTERS:
                        issue_body_str = issue_body_str[:MAX_TICKET_CHARACTERS] + "..."

                    # Extract sub-issues
                    sub_issues_content = []
                    try:
                        sub_issues = git_provider.fetch_sub_issues(ticket)
                        for sub_issue_url in sub_issues:
                            try:
                                sub_repo, sub_issue_number = git_provider._parse_issue_url(sub_issue_url)
                                sub_issue = git_provider.repo_obj.get_issue(sub_issue_number)

                                sub_body = sub_issue.body or ""
                                if len(sub_body) > MAX_TICKET_CHARACTERS:
                                    sub_body = sub_body[:MAX_TICKET_CHARACTERS] + "..."

                                sub_issues_content.append({
                                    'ticket_url': sub_issue_url,
                                    'title': sub_issue.title,
                                    'body': sub_body
                                })
                            except Exception as e:
                                get_logger().warning(f"Failed to fetch sub-issue content for {sub_issue_url}: {e}")

                    except Exception as e:
                        get_logger().warning(f"Failed to fetch sub-issues for {ticket}: {e}")

                    # Extract labels
                    labels = []
                    try:
                        for label in issue_main.labels:
                            labels.append(label.name if hasattr(label, 'name') else label)
                    except Exception as e:
                        get_logger().error(f"Error extracting labels error= {e}",
                                           artifact={"traceback": traceback.format_exc()})

                    tickets_content.append({
                        'ticket_id': issue_main.number,
                        'ticket_url': ticket,
                        'title': issue_main.title,
                        'body': issue_body_str,
                        'labels': ", ".join(labels),
                        'sub_issues': sub_issues_content  # Store sub-issues content
                    })


        # process Trello cards
        for card_id in trello_cards:
            card_details = fetch_trello_card(card_id)
            if card_details:
                tickets_content.append(card_details)

        return tickets_content

    except Exception as e:
        get_logger().error(f"Error extracting tickets error= {e}",
                           artifact={"traceback": traceback.format_exc()})


async def extract_and_cache_pr_tickets(git_provider, vars):
    if not get_settings().get('pr_reviewer.require_ticket_analysis_review', False):
        return

    related_tickets = get_settings().get('related_tickets', [])

    if not related_tickets:
        tickets_content = await extract_tickets(git_provider)

        if tickets_content:
            # Store sub-issues along with main issues
            for ticket in tickets_content:
                if "sub_issues" in ticket and ticket["sub_issues"]:
                    for sub_issue in ticket["sub_issues"]:
                        related_tickets.append(sub_issue)  # Add sub-issues content

                related_tickets.append(ticket)

            get_logger().info("Extracted tickets and sub-issues from PR description",
                              artifact={"tickets": related_tickets})

            vars['related_tickets'] = related_tickets
            get_settings().set('related_tickets', related_tickets)
    else:
        get_logger().info("Using cached tickets", artifact={"tickets": related_tickets})
        vars['related_tickets'] = related_tickets


def check_tickets_relevancy():
    return True
