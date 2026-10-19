import "./PricingPage.css";

const PLANS = [
  { name: "Starter", price: 0, seats: 3, surfaces: "Web" },
  { name: "Team", price: 49, seats: 25, surfaces: "Web + API" },
  { name: "Business", price: 199, seats: 100, surfaces: "Web + API + Runtime" },
];

export default function PricingPage() {
  return (
    <div className="page">
      <h2>Pricing</h2>
      <p className="lede">Plans for teams that review agent-written changes.</p>
      <div className="plans">
        {PLANS.map((plan) => (
          <article key={plan.name} className={plan.featured ? "plan plan--featured" : "plan"}>
            <strong>{plan.name}</strong>
            <span className="plan-price">
              ${plan.price}
              <small>/mo</small>
            </span>
            <span>{plan.seats} seats</span>
            <span>{plan.surfaces}</span>
          </article>
        ))}
      </div>
    </div>
  );
}
