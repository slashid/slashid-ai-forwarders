"""GCP Cloud Run forwarder for Vertex AI ``generateContent``.

Reads request-response logging rows from BigQuery, normalizes each into a
canonical ``NormalizedInvocation`` via the shared ``normalize.gemini``
package, and pushes ``AIInvocationObservedV1`` events to the SlashID NHI
sink. Deployed via the Terraform module under ``vertex/deploy/terraform/``.
"""
