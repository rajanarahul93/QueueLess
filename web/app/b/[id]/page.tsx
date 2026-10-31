import { BusinessDetail } from "@/components/booking/BusinessDetail";

export default async function BusinessPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <section className="mx-auto max-w-[480px] px-4 py-8">
      <BusinessDetail id={id} />
    </section>
  );
}
