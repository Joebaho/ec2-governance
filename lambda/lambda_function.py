import boto3
import csv
import io
import os
import json
import logging
import urllib3
from datetime import datetime, timezone, timedelta

ec2 = boto3.client('ec2')
sns = boto3.client('sns')
s3 = boto3.client('s3')
logger = logging.getLogger()
logger.setLevel(logging.INFO)

SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
REPORT_BUCKET_NAME = os.environ.get("REPORT_BUCKET_NAME", "")
SNAPSHOT_STATES = {
    state.strip() for state in os.environ.get("SNAPSHOT_STATES", "stopped").split(",") if state.strip()
}
TERMINATE_STATES = {
    state.strip() for state in os.environ.get("TERMINATE_STATES", "stopped").split(",") if state.strip()
}

http = urllib3.PoolManager()

def lambda_handler(event, context):
    instances = get_all_instances()
    report = []
    scan_time = datetime.now(timezone.utc).isoformat()
    summary = {}
    actions_taken = []

    for instance in instances:
        instance_id = instance['InstanceId']
        state = instance['State']['Name']
        launch_time = instance.get("LaunchTime")
        tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
        name = tags.get("Name", "")

        policy = evaluate_policy(state, launch_time, tags)
        snapshot_status = "NOT_REQUESTED"
        termination_status = "NOT_REQUESTED"

        if policy["snapshot"]:
            snapshot_count = create_snapshots(instance_id)
            snapshot_status = f"CREATED_{snapshot_count}_SNAPSHOT(S)"

        if policy["terminate"]:
            terminate_instance(instance_id)
            termination_status = "TERMINATION_REQUESTED"

        summary[state] = summary.get(state, 0) + 1
        if policy["snapshot"] or policy["terminate"]:
            actions_taken.append(build_action_line(
                instance_id,
                name,
                snapshot_status,
                termination_status
            ))

        report.append({
            "InstanceId": instance_id,
            "Name": name,
            "State": state,
            "Action": policy["action"],
            "Reason": policy["reason"],
            "SnapshotStatus": snapshot_status,
            "TerminationStatus": termination_status,
            "LaunchTime": launch_time.isoformat() if launch_time else "",
            "Timestamp": scan_time
        })

    csv_report = generate_csv(report)
    report_link = ""
    upload_error = ""

    try:
        report_link = upload_report_to_s3(csv_report, scan_time)
        if report_link:
            logger.info("Uploaded CSV report to S3 successfully")
        else:
            logger.warning("CSV report upload skipped because REPORT_BUCKET_NAME is empty")
    except Exception as exc:
        upload_error = str(exc)
        logger.exception("Failed to upload CSV report to S3")

    slack_message = build_slack_message(
        summary,
        actions_taken,
        len(report),
        scan_time,
        report_link,
        upload_error
    )
    email_message = build_email_message(slack_message, csv_report, report_link, upload_error)

    sns_sent = send_sns(email_message)
    slack_sent = send_slack(slack_message)

    return {
        "statusCode": 200,
        "reportLinkGenerated": bool(report_link),
        "snsSent": sns_sent,
        "slackSent": slack_sent,
        "uploadError": upload_error
    }


def get_all_instances():
    paginator = ec2.get_paginator('describe_instances')
    instances = []

    for page in paginator.paginate():
        for reservation in page['Reservations']:
            for instance in reservation['Instances']:
                instances.append(instance)

    return instances


def evaluate_policy(state, launch_time, tags):
    now = datetime.now(timezone.utc)
    owner_present = "Owner" in tags

    if state == "running":
        if launch_time and (now - launch_time) > timedelta(days=7):
            return policy_result(
                "REVIEW_LONG_RUNNING",
                "Running for more than 7 days",
                snapshot=False,
                terminate=False
            )
        return policy_result("HEALTHY", "Running instance", snapshot=False, terminate=False)

    if state == "stopped":
        return policy_result(
            "SNAPSHOT_AND_TERMINATE" if state in TERMINATE_STATES else "STOPPED_REVIEW",
            "Stopped instance matched governance policy",
            snapshot=state in SNAPSHOT_STATES,
            terminate=state in TERMINATE_STATES
        )

    if state == "terminated":
        return policy_result("IGNORED", "Already terminated", snapshot=False, terminate=False)

    if state in ["pending", "stopping", "shutting-down", "rebooting"]:
        return policy_result("TRANSITION", "Instance is changing state", snapshot=False, terminate=False)

    if not owner_present:
        return policy_result("NON_COMPLIANT", "Missing Owner tag", snapshot=False, terminate=False)

    return policy_result("UNKNOWN", f"Unhandled state: {state}", snapshot=False, terminate=False)


def policy_result(action, reason, snapshot, terminate):
    return {
        "action": action,
        "reason": reason,
        "snapshot": snapshot,
        "terminate": terminate
    }


def create_snapshots(instance_id):
    volumes = ec2.describe_volumes(
        Filters=[{'Name': 'attachment.instance-id', 'Values': [instance_id]}]
    )

    for vol in volumes['Volumes']:
        ec2.create_snapshot(
            VolumeId=vol['VolumeId'],
            Description=f"Auto snapshot for {instance_id}"
        )

    return len(volumes['Volumes'])


def terminate_instance(instance_id):
    ec2.terminate_instances(InstanceIds=[instance_id])


def generate_csv(data):
    output = io.StringIO()
    fieldnames = [
        "InstanceId",
        "Name",
        "State",
        "Action",
        "Reason",
        "SnapshotStatus",
        "TerminationStatus",
        "LaunchTime",
        "Timestamp"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    if data:
        writer.writerows(data)
    return output.getvalue()


def upload_report_to_s3(csv_report, scan_time):
    if not REPORT_BUCKET_NAME:
        return ""

    object_key = build_report_key(scan_time)
    s3.put_object(
        Bucket=REPORT_BUCKET_NAME,
        Key=object_key,
        Body=csv_report.encode("utf-8"),
        ContentType="text/csv"
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": REPORT_BUCKET_NAME, "Key": object_key},
        ExpiresIn=7 * 24 * 60 * 60
    )


def build_report_key(scan_time):
    safe_timestamp = scan_time.replace(":", "-").replace("+00:00", "Z")
    return f"reports/ec2-governance-report-{safe_timestamp}.csv"


def build_slack_message(summary, actions_taken, instance_count, scan_time, report_link, upload_error):
    summary_lines = []
    for state in ["running", "stopped", "pending", "stopping", "shutting-down", "terminated"]:
        if state in summary:
            summary_lines.append(f"- {state.capitalize()}: {summary[state]}")

    extra_states = sorted(set(summary.keys()) - {
        "running", "stopped", "pending", "stopping", "shutting-down", "terminated"
    })
    for state in extra_states:
        summary_lines.append(f"- {state.capitalize()}: {summary[state]}")

    action_lines = actions_taken or ["- No snapshot or termination actions were required"]
    if report_link:
        report_line = f"Download full CSV report: {report_link}\n\n"
    elif upload_error:
        report_line = f"Full CSV report link unavailable. S3 upload error: {upload_error}\n\n"
    else:
        report_line = "Full CSV report link unavailable.\n\n"

    return (
        "CloudSpace EC2 Governance Report\n"
        "Region: us-west-2\n"
        f"Date: {scan_time}\n"
        "Period of scanning: 7 days\n"
        f"Scanned: {instance_count} instances\n\n"
        "Hello everyone,\n\n"
        "Here is the recent report for our environment. Please take a look and get back to me for any questions or concerns.\n\n"
        "Summary\n"
        f"{chr(10).join(summary_lines)}\n\n"
        "Actions Taken\n"
        f"{chr(10).join(action_lines)}\n\n"
        f"{report_line}"
        "Joseph Mbatchou\n"
        "Cloud / DevOps Engineer"
    )


def build_email_message(slack_message, csv_report, report_link, upload_error):
    if report_link:
        return (
            f"{slack_message}"
            f"CSV Download Link: {report_link}\n\n"
            f"Full CSV Report\n{csv_report}"
        )
    if upload_error:
        return (
            f"{slack_message}"
            f"S3 Upload Error: {upload_error}\n\n"
            f"Full CSV Report\n{csv_report}"
        )
    return f"{slack_message}Full CSV Report\n{csv_report}"


def build_action_line(instance_id, name, snapshot_status, termination_status):
    name_suffix = f" ({name})" if name else ""
    details = []

    if snapshot_status.startswith("CREATED_"):
        snapshot_count = snapshot_status.replace("CREATED_", "").replace("_SNAPSHOT(S)", "")
        details.append(f"{snapshot_count} snapshots created")

    if termination_status == "TERMINATION_REQUESTED":
        details.append("termination requested")

    if not details:
        details.append("action recorded")

    return f"- {instance_id}{name_suffix}: {', '.join(details)}"


def send_sns(message):
    if SNS_TOPIC_ARN:
        try:
            sns.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject="EC2 Governance Report",
                Message=message
            )
            logger.info("SNS notification sent successfully")
            return True
        except Exception:
            logger.exception("Failed to send SNS notification")
            return False
    logger.warning("SNS notification skipped because SNS_TOPIC_ARN is empty")
    return False


def send_slack(message):
    if SLACK_WEBHOOK:
        try:
            response = http.request(
                "POST",
                SLACK_WEBHOOK,
                body=json.dumps({"text": message}),
                headers={"Content-Type": "application/json"}
            )
            if response.status >= 400:
                logger.error("Slack webhook returned status %s", response.status)
                return False
            logger.info("Slack notification sent successfully")
            return True
        except Exception:
            logger.exception("Failed to send Slack notification")
            return False
    logger.warning("Slack notification skipped because SLACK_WEBHOOK is empty")
    return False
