import type { SearchOption } from '../common/SearchSelect';

/** auth_mode "credential" with the connecting user's own account instead of a credential-store entry. */
export type PersonalAccountChoice = 'any' | 'user' | 'privileged';

const PREFIX = '@account:';

/** Extra entries at the top of the credential picker in connection / folder settings. */
export const ACCOUNT_CHOICE_OPTIONS: SearchOption[] = [
  { value: `${PREFIX}any`, label: "User's own accounts", hint: 'the user picks one of their accounts' },
  { value: `${PREFIX}user`, label: "User's user account", hint: 'Profile → My accounts' },
  { value: `${PREFIX}privileged`, label: "User's privileged account", hint: 'Profile → My accounts' },
];

/** Picker value for stored settings. */
export function credentialChoiceValue(credentialId: string | null | undefined,
                                      personalAccount: PersonalAccountChoice | null | undefined): string {
  return personalAccount ? `${PREFIX}${personalAccount}` : credentialId ?? '';
}

/** Picker value → the fields to send (credential_id, personal_account). */
export function credentialChoiceFields(value: string): { credential_id: string | null; personal_account: PersonalAccountChoice | null } {
  if (value.startsWith(PREFIX)) return { credential_id: null, personal_account: value.slice(PREFIX.length) as PersonalAccountChoice };
  return { credential_id: value || null, personal_account: null };
}

export const isAccountChoice = (value: string) => value.startsWith(PREFIX);
