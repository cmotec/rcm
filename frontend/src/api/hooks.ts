import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get } from './client';
import type { Credential, CredentialFolder, EffectiveConnection, Me, Page, PersonalAccount, Tree } from './types';

export const keys = {
  me: ['me'] as const,
  tree: ['tree'] as const,
  credentials: ['credentials'] as const,
  credentialFolders: ['credential-folders'] as const,
  personalAccounts: ['personal-accounts'] as const,
  effective: (id: string) => ['effective', id] as const,
};

export function useMe() {
  return useQuery({ queryKey: keys.me, queryFn: () => get<Me>('/api/v1/auth/me'), retry: false, staleTime: 60_000 });
}

export function useTree() {
  return useQuery({ queryKey: keys.tree, queryFn: () => get<Tree>('/api/v1/tree'), staleTime: 15_000 });
}

export function useCredentials(enabled = true) {
  return useQuery({
    queryKey: keys.credentials,
    queryFn: () => get<Page<Credential>>('/api/v1/credentials', { limit: 1000 }),
    enabled,
    staleTime: 15_000,
  });
}

export function useCredentialFolders(enabled = true) {
  return useQuery({
    queryKey: keys.credentialFolders,
    queryFn: () => get<CredentialFolder[]>('/api/v1/credential-folders'),
    enabled,
    staleTime: 15_000,
  });
}

/** The user's own user / privileged account (always both kinds, empty when not set up). */
export function usePersonalAccounts(enabled = true) {
  return useQuery({
    queryKey: keys.personalAccounts,
    queryFn: () => get<PersonalAccount[]>('/api/v1/me/accounts'),
    enabled,
    staleTime: 60_000,
  });
}

/** The folder and every folder below it. */
export function folderSubtree(folders: CredentialFolder[], rootId: string): Set<string> {
  const out = new Set<string>([rootId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const f of folders) {
      if (f.parent_id && out.has(f.parent_id) && !out.has(f.id)) {
        out.add(f.id);
        grew = true;
      }
    }
  }
  return out;
}

/** "Parent / Child / Folder" for a credential folder. */
export function folderPath(folders: CredentialFolder[], id: string | null): string {
  const byId = new Map(folders.map((f) => [f.id, f]));
  const parts: string[] = [];
  for (let f = id ? byId.get(id) : undefined, i = 0; f && i < 64; i++) {
    parts.unshift(f.name);
    f = f.parent_id ? byId.get(f.parent_id) : undefined;
  }
  return parts.join(' / ');
}

export function useEffective(id: string | null) {
  return useQuery({
    queryKey: keys.effective(id ?? ''),
    queryFn: () => get<EffectiveConnection>(`/api/v1/connections/${id}/effective`),
    enabled: Boolean(id),
  });
}

export function useInvalidate() {
  const qc = useQueryClient();
  return {
    tree: () => {
      void qc.invalidateQueries({ queryKey: keys.tree });
      void qc.invalidateQueries({ queryKey: ['effective'] });
    },
    credentials: () => {
      void qc.invalidateQueries({ queryKey: keys.credentials });
      void qc.invalidateQueries({ queryKey: keys.credentialFolders });
    },
    all: () => void qc.invalidateQueries(),
  };
}

export function hasPerm(me: Me | undefined, perm: string): boolean {
  return Boolean(me && (me.is_admin || me.permissions.includes(perm)));
}
