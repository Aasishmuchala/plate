// A custody grade as a coloured chip: C0 green, C1 blue, C2 amber, D red (docs/custody.md).

import type { CustodyGrade } from "../nodeTypes";
import { CUSTODY_TONE } from "./quote";

export function CustodyChip({ grade }: { grade: CustodyGrade }) {
  return (
    <span className={`chip chip-custody tone-${CUSTODY_TONE[grade]}`} data-testid="quote-custody" data-grade={grade}>
      {grade}
    </span>
  );
}
