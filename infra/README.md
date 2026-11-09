# Project-scoped AWS permissions

Use a dedicated project profile rather than giving an agent AdministratorAccess.
The application never invokes IAM or creates credentials. Have the account's
authorized administrator review and attach the template through the approved
human account-administration process.

In `iam-policy.template.json`, replace `ACCOUNT_ID`, `BUCKET_NAME`, `DOMAIN_NAME`,
`PROJECT_ID`, `BINDING` and `HOSTED_ZONE_ID`. The binding is printed in each plan.
Use a lowercase domain without a trailing dot. Restrict the policy to the actual
resources once their IDs are known.

The hosted-zone ID is not known during first-time bootstrap. For foundation only,
the administrator can temporarily scope `route53:GetHostedZone` to
`arn:aws:route53:::hostedzone/*`, then substitute the exact created zone before
DNS changes. Foundation does not write DNS records. Remove resource-creation
statements after bootstrap if no further creation is needed. AWS list/create
actions that cannot be resource-scoped use `Resource: "*"`, with tag/domain
conditions where supported; they do not grant unrelated services or deletion.

Application checks separately limit record names, object paths, resource tags,
approved stages and ETags. The template is not claimed to have been attached or
tested in your account. Account policies and permission boundaries can impose
additional restrictions; resolve AccessDenied with only the needed project
permissions through the authorized administrator.

References: [CloudFront authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_cloudfront.html),
[ACM authorization](https://docs.aws.amazon.com/service-authorization/latest/reference/list_acm.html),
[Route 53 record conditions](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/specifying-conditions-route53.html).
