import { useState, type FormEvent } from "react";
import type { CorrectableFieldName, Loan, LoanCorrection } from "../api/types";

export const CORRECTABLE_FIELDS: Array<{ name: CorrectableFieldName; label: string }> = [
  { name: "sanctioned_amount", label: "Sanctioned amount" },
  { name: "disbursed_amount", label: "Disbursed amount" },
  { name: "repayment_amount", label: "Repayment amount" },
  { name: "processing_fee", label: "Processing fee" },
  { name: "gst_amount", label: "GST amount" },
  { name: "other_fees_total", label: "Other fees" },
  { name: "sanction_date", label: "Sanction date" },
  { name: "disbursement_date", label: "Disbursement date" },
  { name: "repayment_date", label: "Repayment date" },
  { name: "lender_brand", label: "Lender" },
  { name: "legal_lending_entity", label: "Legal lender" },
];

export function correctionPatch(fieldName: string, value: string): LoanCorrection {
  return { [fieldName]: value } as LoanCorrection;
}

/** Show the extra rupees and percent returned by a correction. Omit either piece when null. */
export function formatReturnedExtra(loan: Loan): string | null {
  const rupees = loan.calculated?.implied_cost?.value ?? null;
  const percent = loan.calculated?.extra_percent?.value ?? null;
  if (rupees === null || percent === null || rupees === "" || percent === "") {
    return null;
  }
  const rupeeText = rupees.endsWith(".00") ? rupees.slice(0, -3) : rupees;
  return `extra ${rupeeText} (${percent}%)`;
}

interface CorrectionLoan {
  loan_internal_id: string;
  label: string;
}

interface FieldCorrectionFormProps {
  loans: CorrectionLoan[];
  disabled?: boolean;
  extraText: string | null;
  onSave: (loanId: string, patch: LoanCorrection) => void;
}

export function FieldCorrectionForm({ loans, disabled = false, extraText, onSave }: FieldCorrectionFormProps) {
  const [loanId, setLoanId] = useState(loans[0]?.loan_internal_id ?? "");
  const [fieldName, setFieldName] = useState<CorrectableFieldName>("disbursed_amount");
  const [value, setValue] = useState("");
  const selectedLoan = loans.some((loan) => loan.loan_internal_id === loanId)
    ? loanId
    : (loans[0]?.loan_internal_id ?? "");

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = value.trim();
    if (selectedLoan === "" || trimmed === "") {
      return;
    }
    onSave(selectedLoan, correctionPatch(fieldName, trimmed));
  }

  if (loans.length === 0) {
    return null;
  }

  return (
    <form className="correction-form" onSubmit={onSubmit}>
      <h2>Correct a value</h2>
      {loans.length > 1 ? (
        <label>
          Loan
          <select
            aria-label="Loan to correct"
            value={selectedLoan}
            disabled={disabled}
            onChange={(event) => setLoanId(event.target.value)}
          >
            {loans.map((loan) => (
              <option key={loan.loan_internal_id} value={loan.loan_internal_id}>
                {loan.label}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <label>
        Field
        <select
          aria-label="Field to correct"
          value={fieldName}
          disabled={disabled}
          onChange={(event) => setFieldName(event.target.value as CorrectableFieldName)}
        >
          {CORRECTABLE_FIELDS.map((field) => (
            <option key={field.name} value={field.name}>
              {field.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        New value
        <input
          aria-label="New value"
          value={value}
          disabled={disabled}
          onChange={(event) => setValue(event.target.value)}
        />
      </label>
      <button className="secondary-button" type="submit" disabled={disabled || value.trim() === ""}>
        Save correction
      </button>
      {extraText ? <p>{extraText}</p> : null}
    </form>
  );
}
