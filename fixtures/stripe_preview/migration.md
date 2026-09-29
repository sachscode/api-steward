# Curated teaching note: Stripe invoice-preview mapping

This compact note comes from the agreed project plan. It is not live retrieval
or a complete Stripe migration guide. The source application and proposed
baseline still require qualification.

- Proposed baseline: stripe-python 11.6.0, API 2025-02-24.acacia.
- Target: stripe-python 12.0.0, API 2025-03-31.basil.
- Replace `stripe.Invoice.upcoming` with `stripe.Invoice.create_preview`.
- Map `subscription_items` into `subscription_details.items`.
- Preserve customer, subscription, item, price and quantity inputs.
- Independently verify preview totals, proration, discounts, UI fields,
  provider failures and absence of billing mutations before accepting a patch.

References for the later real migration:
https://github.com/stripe/stripe-python/wiki/Migration-guide-for-v12
https://docs.stripe.com/api/invoices/create_preview?api-version=2025-03-31.basil
