import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get } from './client';
import type { Credential, EffectiveConnection, Me, Page, Tree } from './types';

export const keys = {
  me: ['me'] as const,
  tree: ['tree'] as const,
  credentials: ['credentials'] as const,
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
    credentials: () => void qc.invalidateQueries({ queryKey: keys.credentials }),
    all: () => void qc.invalidateQueries(),
  };
}

export function hasPerm(me: Me | undefined, perm: string): boolean {
  return Boolean(me && (me.is_admin || me.permissions.includes(perm)));
}
