"""
Create a table of our cloud bills across all our AWS bills, which is
then posted to Slack.

The table looks something like:

    account                  prev 3 months av.    last month ($)
    ---------------------  -------------------  ----------------  ------  ------
    STEM prod                         1,234.24          1,234.83          ↟↟ 61%
    STEM staging                      1,234.35          1,234.40   ↓  5%
    Kano prod operations              1,234.23          1,234.83   ↓ 17%
    Kano data                           123.33            123.03
    Kano services sandbox                12.02             12.12          ↟↟  5%
    STEM communicator dev                12.17             12.90          ↟↟ 16%
    Kano pc                              20.25              6.93   ↓ 65%
    Kano sandbox                          1.72              1.72
    Kano backups                          0.48              0.48
    Kano os                               0.08              0.08
    Kano ops admin                        1.13              0.01   ↓ 98%
    Kano ecommerce                        0.00              0.00   ↓ 75%
    -------------          -------------------  ----------------  ------  ------
    TOTAL                            xx,xxx.xx         xx,xxx.xx          ↟↟ 19%

How it works:

-   We create an IAM role in each of our AWS acounts that gives the
    ce:GetCostAndUsage permission for that account.
-   We give the task role for this Lambda permission to assume that role.
-   When the Lambda runs, it goes through the accounts in turn, and assumes
    the corresponding role.  It gets the unblended costs for that account.
-   The Lambda uses the tabulate library to render the table.

It also fetches our Elastic Cloud bill, using an Elastic Cloud API key we
keep in Secrets Manager.

"""

import collections
import datetime
import json
import os
import re
import urllib.error
import urllib.request

import boto3
import tabulate


DEFAULT_ROLE_ARN = 'arn:aws:iam::370771240857:role/kano'


def get_aws_session(*, role_arn):
    """
    Get a boto3 Session authenticated with the given role ARN.
    """
    sts_client = boto3.client("sts")
    assumed_role_object = sts_client.assume_role(
        RoleArn=role_arn, RoleSessionName="AssumeRoleSession1"
    )
    credentials = assumed_role_object["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )


def get_last_four_months_of_bills(*, account_id, role_arn=DEFAULT_ROLE_ARN):
    """
    Retrieve the last four months of total costs for the given AWS role.
    """
    sess = get_aws_session(role_arn=role_arn)
    client = sess.client("ce")

    this_month_start = datetime.date.today().replace(day=1)

    if this_month_start.month > 4:
        four_months_ago = this_month_start.replace(month=this_month_start.month - 4)
    else:
        four_months_ago = this_month_start.replace(
            year=this_month_start.year - 1, month=12 - this_month_start.month
        )

    resp = client.get_cost_and_usage(
        TimePeriod={
            "Start": four_months_ago.isoformat(),
            "End": this_month_start.isoformat(),
        },
        Granularity="MONTHLY",
        # Note: I'm using unblended costs because it matches the number
        # I see in the console.  It may not be the most correct number
        # from an accounting POV or what we actually pay AWS, but for
        # this tool that doesn't matter.
        #
        # We care about the *direction* of the bill, not the exact amount.
        # I'm assuming that a 10% rise in spend would be reflected in
        # all forms of amortised/blended/normalised/unblended costs, and
        # that change is what I care about.
        #
        # Metrics=["UnblendedCost"],
        Metrics=["NetAmortizedCost"],
        Filter={
            'Dimensions': {
                'Key': 'LINKED_ACCOUNT',
                'Values': [
                    account_id
                ]
            }
        }
    )

    result = {}

    for entry in resp["ResultsByTime"]:
        start = datetime.datetime.strptime(entry["TimePeriod"]["Start"], "%Y-%m-%d")
        # assert entry["Total"]["UnblendedCost"]["Unit"] == "USD"
        assert entry["Total"]["NetAmortizedCost"]["Unit"] == "USD"
        result[(start.year, start.month)] = float(
            # entry["Total"]["UnblendedCost"]["Amount"]
            entry["Total"]["NetAmortizedCost"]["Amount"]
        )

    return result


def get_secret_string(sess, *, secret_id):
    """
    Look up the value of a SecretString in Secrets Manager.
    """
    client = sess.client("secretsmanager")
    return client.get_secret_value(SecretId=secret_id)["SecretString"]


def _get_elastic_cloud_costs_for_range(*, from_date, to_date, api_key, organisation_id):
    # See https://www.elastic.co/guide/en/cloud/current/Billing_Costs_Analysis.html
    url = f"https://api.elastic-cloud.com/api/v1/billing/costs/{organisation_id}?from={from_date.isoformat()}T00:00:00Z&to={to_date.isoformat()}T00:00:00Z"
    headers = {"Authorization": f"ApiKey {api_key}"}
    req = urllib.request.Request(url, headers=headers)

    resp = urllib.request.urlopen(req)
    result = json.load(resp)

    return result["costs"]["total"]


def get_elastic_cloud_bill(date_blocks):
    """
    Retrieve the last four months of total costs for Elastic Cloud.
    """
    sess = boto3.Session()

    api_key = get_secret_string(sess, secret_id="elastic_cloud/api_key")
    organisation_id = get_secret_string(sess, secret_id="elastic_cloud/organisation_id")

    result = {}

    for year, month in date_blocks:
        from_date = datetime.date(year, month, day=1)

        if month == 12:
            to_date = datetime.date(year + 1, month=1, day=1)
        else:
            to_date = datetime.date(year, month + 1, day=1)

        try:
            result[(year, month)] = _get_elastic_cloud_costs_for_range(
                from_date=from_date,
                to_date=to_date,
                api_key=api_key,
                organisation_id=organisation_id,
            )
        except urllib.error.HTTPError:
            # Elastic Cloud only supports getting up to three months of
            # billing data with this API at time of writing.
            #
            # If we do get an error, assume it's because we exceeded three
            # months, and mark the cost as zero.  If Elastic ever extend
            # the supported range, this will start working.
            result[(year, month)] = 0

    return result


def average(values):
    return sum(values) / len(values)


def pprint_currency(v):
    if isinstance(v, float):
        orig = "%.2f" % v
    else:
        orig = v

    new = re.sub(r"^(-?\d+)(\d{3})", fr"\g<1>,\g<2>", orig)
    if orig == new:
        return new
    else:
        return pprint_currency(new)


def _render_row(label, per_month_bills):
    *prev_months, this_month = sorted(per_month_bills.items())

    # We skip 0 values when computing the average -- this either means
    # the account didn't exist in that month, or we don't have billing
    # data for that month.
    prev_month_average = average([total for _, total in prev_months if total != 0])

    _, this_month_total = this_month

    # If 95% of the previous month is bigger than this month, then
    # we've saved at least 5%
    if prev_month_average * 0.95 > this_month_total:
        reduction = int((1 - this_month_total / prev_month_average) * 100)
        gain, loss = f"↓ {reduction:2d}%", ""

    # If 105% of the previous months is less than this month, then
    # we've spent at least 5% more
    elif prev_month_average * 1.05 <= this_month_total:
        extra_spend = int((this_month_total / prev_month_average - 1) * 100)
        gain, loss = "", f"↟↟ {extra_spend:2d}%"

    else:
        gain, loss = "", ""

    return [
        label,
        pprint_currency(prev_month_average),
        pprint_currency(this_month_total),
        gain,
        loss,
    ]


def create_billing_table(billing_data):
    """
    Returns a string that contains a table that describes the billing data.

    This table is meant for printing in a monospaced font.
    """
    rows = [
        _render_row(account_name, per_month_bills)
        for account_name, per_month_bills in billing_data.items()
    ]

    # Sort the rows so the most expensive account is at the top
    rows = sorted(rows, key=lambda r: float(r[2].replace(",", "")), reverse=True)

    # Add a footer row that shows the total.
    total_bills = collections.defaultdict(int)
    for name, per_month_bills in billing_data.items():
        for month, amount in per_month_bills.items():

            # We may have some zero-values in here; if so, backfill with
            # the average of the non-zero bills for this month.  This avoids
            # any zero values dragging down the average.
            if amount == 0:
                total_bills[month] += average(
                    [v for v in per_month_bills.values() if v > 0]
                )
            else:
                total_bills[month] += amount

    rows.append(
        ["-------------", "-------------------", "----------------", "------", "------"]
    )
    rows.append(_render_row("TOTAL", total_bills))

    return tabulate.tabulate(
        rows,
        headers=["account", "prev 3 months av.", "last month ($)", "", ""],
        floatfmt=".2f",
        colalign=("left", "right", "right", "right", "right"),
    )


def main(_event=None, _context=None):
    billing_data = {}

    running_in_lambda = os.environ.get("AWS_EXECUTION_ENV", "").startswith(
        "AWS_Lambda_"
    )

    for account_id, account_name in [
        # ("030469704569", "kano-prod"),
        # ("514333761684", "kano-staging"),
        # ("490793685774", "kano-artopia"),
        ("271119165879", "STEM prod"),
        ("044436578076", "STEM staging"),
        ("370771240857", "Kano prod operations"),
        ("214499854237", "Kano data"),
        ("169316547066", "Kano services sandbox"),
        ("648433460441", "STEM communicator dev"),
        ("802610295201", "Kano PC"),
        ("245070913529", "Kano sandbox"),
        ("657385723538", "Kano backups"),
        ("184856031229", "Kano OS"),
        ("602638832636", "Kano ops/admin"),
        ("446458378801", "Kano ecommerce"),
    ]:
        # if running_in_lambda:
        #     role_arn = (
        #         f"arn:aws:iam::{account_id}:role/{account_name}-costs_report_lambda"
        #     )
        # else:
        #     role_arn = f"arn:aws:iam::{account_id}:role/{account_name}-developer"

        # role_arn = f"arn:aws:iam::{account_id}:role/{account_name}"

        billing_data[account_name] = get_last_four_months_of_bills(account_id=account_id)

    # billing_data["elastic cloud"] = get_elastic_cloud_bill(
    #     date_blocks=billing_data["platform"].keys()
    # )

    table = create_billing_table(billing_data)

    if not running_in_lambda:
        print(table)
        return

    this_month = datetime.date.today() - datetime.timedelta(days=28)

    slack_payload = {
        "username": "costs-report",
        "icon_emoji": ":money_with_wings:",
        "attachments": [
            {
                "title": f"Costs report for {this_month.strftime('%B %Y')}",
                "fields": [{"value": f"```\n{table}\n```"}],
            }
        ],
    }

    # A Slack hook API key. To be used to post AWS cost reports to the Slack channel, '#aws_cost_reports'
    # Stored in AWS Secrets Manager. Region is us-west-2
    sess = boto3.Session()
    webhook_url = get_secret_string(sess, secret_id="slack/aws-costs-report-platform-hook")

    print("Sending message %s" % json.dumps(slack_payload), flush=True)

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(slack_payload).encode("utf8"),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200, resp


if __name__ == "__main__":
    main()
