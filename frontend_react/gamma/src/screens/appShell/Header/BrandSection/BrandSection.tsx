import { brandSectionData } from "../headerContent";

function BrandSection() {
  return (
    <div className="flex h-full min-w-0 gap-3 ">

      <div className="min-w-0">
        <div className="flex flex-wrap ">
          <h1 className="text-md font-semibold tracking-[0.02em] text-black sm:text-lg">
            {brandSectionData.name}
          </h1>
        </div>

        <p className=" text-xs leading-5 text-gray-900">
          {brandSectionData.subtitle}
        </p>
      </div>
    </div>
  );
}

export default BrandSection;
