output "all_gemini_models" {
  description = "Every ``google/gemini-*`` publisher model currently listed by Vertex Model Garden for the project."
  value       = local.gemini_models
}

output "all_models" {
  description = "Every publisher model currently listed by Vertex Model Garden for the project (spans google, anthropic, meta, mistralai, ai21, and any others Google has onboarded). Only Gemini generateContent is supported by the forwarder in phase 3.1 — enrolling non-Gemini publishers today means enabling BQ logging on models the forwarder cannot yet process."
  value       = local.all_models
}
