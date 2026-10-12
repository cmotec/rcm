// guacamole-common-js ships an ESM build with a *default* export, while @types/guacamole-common-js
// describes named exports. Normalise both shapes here; import Guacamole from this module only.
import * as GuacNs from 'guacamole-common-js';

type GuacamoleNamespace = typeof GuacNs;

const Guacamole: GuacamoleNamespace =
  ((GuacNs as unknown as { default?: GuacamoleNamespace }).default ?? GuacNs) as GuacamoleNamespace;

export default Guacamole;
export type { GuacNs as GuacTypes };
