import Link from "next/link";

export type BreadcrumbItem = Readonly<{ label: string; href?: string }>;

export function Breadcrumbs({ items, label = "Breadcrumbs" }: Readonly<{ items: BreadcrumbItem[]; label?: string }>) {
  return (
    <nav className="breadcrumbs" aria-label={label}>
      <ol>
        {items.map((item, index) => (
          <li key={`${item.label}-${index}`}>
            {item.href ? <Link href={item.href}>{item.label}</Link> : <span aria-current="page">{item.label}</span>}
          </li>
        ))}
      </ol>
    </nav>
  );
}
