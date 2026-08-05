import { useState, useRef, useEffect } from 'react';
import { 
  Container, Title, Text, Tabs, Grid, Paper, Group, ActionIcon, 
  useMantineColorScheme, Stack, Image, Collapse, Button, Table, 
  Tooltip, Box, List, Anchor, Divider 
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { useMutation, useQuery } from '@tanstack/react-query';
import { IconSun, IconMoon, IconFlask, IconAtom, IconEye, IconEyeOff, IconX, IconDownload } from '@tabler/icons-react';

import { useDebouncedCallback } from '@mantine/hooks';
import { Ketcher, KetcherRef } from './components/Ketcher';
import { MoleculeForm } from './components/MoleculeForm';
import { SiteForm } from './components/SiteForm';
import { ResultsTable } from './components/ResultsTable';
import { submitMoleculeJob, submitSiteJob, getJobStatus, EnumerationResult, getMolWithIndices, cancelJob, getServerMode, getDownloadUrl, convertSmilesToMol } from './api';

interface MolReferenceData {
    svg: string;
    properties?: {
        MW: number;
        LogP: number;
        HBA: number;
        HBD: number;
        TPSA: number;
        QED: number;
    };
}

function App() {
  const { colorScheme, toggleColorScheme } = useMantineColorScheme();
  const ketcherRef = useRef<KetcherRef>(null);
  
  const [activeTab, setActiveTab] = useState<string | null>('molecule');
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const [results, setResults] = useState<EnumerationResult[]>([]);
  const [isMultiFragment, setIsMultiFragment] = useState(false);
  const [serverMode, setServerMode] = useState<'tasks' | 'local'>('local');
  
  const [molRefData, setMolRefData] = useState<MolReferenceData | null>(null);
  const [showIndices, setShowIndices] = useState(true);
  const [initialMolfile, setInitialMolfile] = useState<string | undefined>(undefined);

  // Fetch server mode and example molecule on mount
  useEffect(() => {
    getServerMode().then(setServerMode).catch(() => setServerMode('local'));
    
    const exampleSmiles = 'COC1=CC=CC=C1OCCNCC(COC2=CC=CC3=C2C4=CC=CC=C4N3)O';
    convertSmilesToMol(exampleSmiles).then(setInitialMolfile).catch(console.error);
  }, []);

  // --- Mutations ---

  const cancelMutation = useMutation({
    mutationFn: cancelJob,
    onSuccess: () => {
      notifications.show({ title: 'Cancelled', message: 'Job has been cancelled', color: 'orange' });
      setCurrentJobId(null);
    },
    onError: (err: any) => {
      notifications.show({ title: 'Error', message: err.message || 'Failed to cancel job', color: 'red' });
    }
  });

  const moleculeMutation = useMutation({
    mutationFn: submitMoleculeJob,
    onSuccess: (data) => {
      setCurrentJobId(data.job_id);
      notifications.show({ title: 'Molecule Job Submitted', message: 'Enumeration started...', color: 'blue' });
      setResults([]);
    },
    onError: (err: any) => {
      notifications.show({ title: 'Error', message: err.message || 'Failed to submit job', color: 'red' });
    }
  });

  const siteMutation = useMutation({
    mutationFn: submitSiteJob,
    onSuccess: (data) => {
      setCurrentJobId(data.job_id);
      notifications.show({ title: 'Site Job Submitted', message: 'Enumeration started...', color: 'violet' });
      setResults([]);
    },
    onError: (err: any) => {
      notifications.show({ title: 'Error', message: err.message || 'Failed to submit job', color: 'red' });
    }
  });

  // --- Polling ---

  const jobQuery = useQuery({
     queryKey: ['jobStatus', currentJobId],
     queryFn: () => getJobStatus(currentJobId!),
     enabled: !!currentJobId,
     refetchInterval: (data) => {
        if (data?.state.data?.status === 'SUCCESS' || data?.state.data?.status === 'FAILURE') return false;
        return 2000;
     }
  });

  useEffect(() => {
    if (jobQuery.data?.status === 'SUCCESS' && results.length === 0 && jobQuery.data.result) {
        const displayResults = jobQuery.data.result.display || [];
        setResults(displayResults);
        
        if (displayResults.length <= 1) {
            notifications.show({
              title: 'No New Molecules Found',
              message: 'Only the input molecule was returned. Try adjusting the parameters.',
              color: 'orange',
            });
        } else {
            notifications.show({
              title: 'Success',
              message: `Enumeration completed! Generated ${displayResults.length} molecules.`,
              color: 'green',
            });
        }
    } else if (jobQuery.data?.status === 'FAILURE' && currentJobId) {
        notifications.show({
            title: 'Job Failed',
            message: jobQuery.data.error || 'Unknown error occurred',
            color: 'red',
        });
        setCurrentJobId(null);
    }
  }, [jobQuery.data, results.length, currentJobId]);

  // --- Handlers ---

  const handleStructChange = useDebouncedCallback(async (smiles: string) => {
      const dotCount = (smiles.match(/\./g) || []).length;
      setIsMultiFragment(dotCount >= 1);
      
      // Fetch atom indices visualization
      if (smiles) {
          try {
              const data = await getMolWithIndices(smiles);
              // Normalize data if properties are missing (backward compat)
              if (typeof data === 'string') {
                   setMolRefData({ svg: data });
              } else {
                   setMolRefData(data);
              }
          } catch (e) {
              console.error("Failed to render indices", e);
          }
      } else {
          setMolRefData(null);
      }
  }, 500);

  const handleMoleculeSubmit = async (values: any) => {
      if (!ketcherRef.current) return;
      try {
          const smiles = await ketcherRef.current.getSmiles();
          if (!smiles) {
              notifications.show({ title: 'Error', message: 'Please draw a molecule first', color: 'red' });
              return;
          }
          moleculeMutation.mutate({ ...values, molecule: smiles });
      } catch (e) {
          notifications.show({ title: 'Error', message: 'Failed to retrieve structure', color: 'red' });
      }
  };

  const handleSiteSubmit = async (values: any) => {
      if (!ketcherRef.current) return;
      try {
          const smiles = await ketcherRef.current.getSmiles();
          if (!smiles) {
              notifications.show({ title: 'Error', message: 'Please draw a molecule first', color: 'red' });
              return;
          }
          siteMutation.mutate({ ...values, molecule: smiles });
      } catch (e) {
          notifications.show({ title: 'Error', message: 'Failed to retrieve structure', color: 'red' });
      }
  };

  const isLoading = moleculeMutation.isPending || siteMutation.isPending || (!!currentJobId && jobQuery.data?.status !== 'SUCCESS' && jobQuery.data?.status !== 'FAILURE');

  return (
    <Container fluid p="md" style={{ minHeight: '100vh', backgroundColor: colorScheme === 'dark' ? '#1A1B1E' : '#f8f9fa' }}>
      
      {/* Header */}
      <Box mb="xl" pos="relative">
        <Stack align="center" gap={0}>
          <Group gap="md" align="center">
            <Image src="/healer_logo_no_text.png" alt="HEALER" h={36} w="auto" fit="contain" />
            <Title order={2}>HEALER Dashboard</Title>
            <Image src="/healer_logo_no_text.png" alt="HEALER" h={36} w="auto" fit="contain" />
          </Group>
          <Text c="dimmed">Molecular enumeration using retrosynthetic analysis</Text>
        </Stack>
        <ActionIcon 
          variant="outline" 
          color={colorScheme === 'dark' ? 'yellow' : 'blue'} 
          onClick={() => toggleColorScheme()} 
          title="Toggle color scheme"
          pos="absolute"
          right={0}
          top="50%"
          style={{ transform: 'translateY(-50%)' }}
        >
          {colorScheme === 'dark' ? <IconSun size="1.1rem" /> : <IconMoon size="1.1rem" />}
        </ActionIcon>
      </Box>

      {/* Project Description */}
      <Paper withBorder p="md" mb="xl" radius="md" shadow="xs">
        <Stack gap="xs">
          <Text size="sm">
            <strong>HEALER</strong> (Hit Expansion to Advanced Leads Using Enumerated Reactions) is a 
            computational chemistry platform designed to accelerate lead optimization by generating 
            synthetically accessible chemical space.
          </Text>
          
          <Text size="sm" fw={500}>Capabilities:</Text>
          <List size="sm" withPadding>
            <List.Item>
              <strong>Molecule HEALER</strong>: Deconstruct a query molecule into its constituent 
              fragments and re-enumerate them to discover novel analogs with improved properties.
            </List.Item>
            <List.Item>
              <strong>Fragment HEALER</strong>: Enumerate analogs for a set of fragments (e.g., from a 
              multi-fragment query) to explore chemical space around specific motifs.
            </List.Item>
            <List.Item>
              <strong>Site HEALER</strong>: Perform targeted enumeration at specific reactive sites to 
              explore local SAR (Structure-Activity Relationship) while maintaining the core scaffold.
            </List.Item>
          </List>

          <Text size="sm">
            Generated molecules are designed to be synthetically accessible through established 
            reaction transforms, bridging the gap between computational design and laboratory synthesis. 
            This server uses <Anchor href="https://enamine.net/building-blocks/building-blocks-catalog" target="_blank">Enamine's publicly available building blocks</Anchor> for 
            enumeration. However, HEALER is not limited to Enamine's libraries and can be used with any 
            custom building block collection when set up locally.
          </Text>

          <Text size="sm">
            The reaction library is available <Anchor href="https://github.com/eneskelestemur/healer/blob/main/healer/data/reactions/reactions.json" target="_blank">here</Anchor>. 
            Please reach out to us if you wish to add custom reaction SMARTS. You can learn more about the reaction library format 
            at <Anchor href="https://github.com/eneskelestemur/healer?tab=readme-ov-file#contributing-reactions" target="_blank">Contributing Reactions</Anchor>.
          </Text>

          <Text size="sm">
            To raise issues, suggest features, contribute to the project, or open discussions, please use 
            the <Anchor href="https://github.com/eneskelestemur/healer/issues" target="_blank">GitHub Issues Page</Anchor>.
          </Text>

          <Divider variant="dashed" label="Usage & Privacy" labelPosition="center" />

          <Text size="xs" c="dimmed">
            <strong>Note:</strong> This web interface applies certain limits to input parameters to ensure stable performance. 
            For unrestricted access, please follow the setup instructions on <Anchor href="https://github.com/eneskelestemur/healer" target="_blank">GitHub</Anchor> to 
            use HEALER via the CLI, Python API, or a local UI instance.
          </Text>

          <Text size="xs" c="dimmed">
            <strong>Privacy:</strong> This server does not collect or store your query molecules or generated results. 
            All processing is performed in-memory or via temporary job queues.
          </Text>

          <Group gap="xs" mt="xs">
            <Anchor href="https://doi.org/10.26434/chemrxiv.15003011/v1" size="xs" target="_blank" fw={700}>HEALER Paper</Anchor>
            <Text size="xs" c="dimmed">|</Text>
            <Anchor href="https://github.com/eneskelestemur/healer" size="xs" target="_blank" fw={700}>HEALER GitHub Repo</Anchor>
          </Group>
        </Stack>
      </Paper>

      {/* Tabs */}
      <Tabs value={activeTab} onChange={setActiveTab} variant="pills" radius="md" mb="md">
        <Tabs.List grow>
          <Tabs.Tab value="molecule" color="blue" leftSection={<IconAtom size={16} />}>
            Molecule HEALER
          </Tabs.Tab>
          <Tabs.Tab value="site" color="violet" leftSection={<IconFlask size={16} />}>
            Site HEALER
          </Tabs.Tab>
        </Tabs.List>

        <Grid mt="md">
             {/* Left Column: Form & Settings */}
             <Grid.Col span={{ base: 12, md: 5, lg: 4 }}>
                 <Paper p="md" withBorder shadow="sm">
                    <Title order={4} mb="md">Parameters</Title>
                    
                    <Tabs.Panel value="molecule">
                        <MoleculeForm 
                            onSubmit={handleMoleculeSubmit} 
                            isLoading={isLoading} 
                            isMultiFragment={isMultiFragment}
                        />
                    </Tabs.Panel>

                    <Tabs.Panel value="site">
                        <SiteForm 
                            onSubmit={handleSiteSubmit}
                            isLoading={isLoading}
                        />
                    </Tabs.Panel>
                 </Paper>
             </Grid.Col>

             {/* Right Column: Editor & Results */}
             <Grid.Col span={{ base: 12, md: 7, lg: 8 }}>
                 <Stack gap="md">
                     <Paper withBorder shadow="sm" p={0} style={{ overflow: 'hidden' }}>
                         <Ketcher 
                            ref={ketcherRef} 
                            onStructChange={handleStructChange} 
                            initialMolfile={initialMolfile}
                         />
                     </Paper>

                     {/* Atom Indices Visualizer */}
                     {molRefData && (
                         <Paper withBorder p="xs">
                             <Group justify="space-between" mb="xs">
                                 <Text size="sm" fw={500}>Atom Indices Reference (for Reactive/Split Sites)</Text>
                                 <Button 
                                     variant="subtle" size="xs" 
                                     leftSection={showIndices ? <IconEyeOff size={14}/> : <IconEye size={14}/>}
                                     onClick={() => setShowIndices(!showIndices)}
                                 >
                                     {showIndices ? 'Hide' : 'Show'}
                                 </Button>
                             </Group>
                             <Collapse in={showIndices}>
                                 <Grid>
                                    <Grid.Col span={6}>
                                        <Image src={molRefData.svg} fit="contain" bg="white" p="xs" />
                                    </Grid.Col>
                                    <Grid.Col span={6}>
                                        <Text size="xs" fw={700} mb={5}>Properties</Text>
                                        {molRefData.properties && (
                                            <Table withTableBorder withColumnBorders>
                                                <Table.Tbody>
                                                    <Table.Tr><Table.Td>MW</Table.Td><Table.Td>{molRefData.properties.MW}</Table.Td></Table.Tr>
                                                    <Table.Tr><Table.Td>LogP</Table.Td><Table.Td>{molRefData.properties.LogP}</Table.Td></Table.Tr>
                                                    <Table.Tr><Table.Td>HBA</Table.Td><Table.Td>{molRefData.properties.HBA}</Table.Td></Table.Tr>
                                                    <Table.Tr><Table.Td>HBD</Table.Td><Table.Td>{molRefData.properties.HBD}</Table.Td></Table.Tr>
                                                    <Table.Tr><Table.Td>TPSA</Table.Td><Table.Td>{molRefData.properties.TPSA}</Table.Td></Table.Tr>
                                                    <Table.Tr><Table.Td>QED</Table.Td><Table.Td>{molRefData.properties.QED}</Table.Td></Table.Tr>
                                                </Table.Tbody>
                                            </Table>
                                        )}
                                    </Grid.Col>
                                 </Grid>
                             </Collapse>
                         </Paper>
                     )}
                     
                     <Paper p="md" withBorder shadow="sm">
                        <Group justify="space-between" mb="sm">
                          <Title order={4} mb="sm">Results {results.length > 0 && `(${results.length})`}</Title>
                          {jobQuery.data?.status === 'SUCCESS' && currentJobId && (
                              <Group justify="flex-end">
                                  <Button 
                                      component="a" 
                                      href={getDownloadUrl(currentJobId)} 
                                      leftSection={<IconDownload size={16} />}
                                      variant="outline"
                                      color="green"
                                  >
                                      Download Results (CSV)
                                  </Button>
                              </Group>
                          )}
                        </Group>
                        {isLoading && (
                          <Group gap="sm" mb="sm">
                            <Text>Processing...</Text>
                            <Tooltip 
                              label={serverMode === 'local' ? 'Cancel not available in local mode' : 'Cancel job'}
                              withArrow
                            >
                              <Button
                                size="xs"
                                color="red"
                                variant="light"
                                leftSection={<IconX size={14} />}
                                disabled={serverMode === 'local' || !currentJobId}
                                loading={cancelMutation.isPending}
                                onClick={() => currentJobId && cancelMutation.mutate(currentJobId)}
                              >
                                Cancel
                              </Button>
                            </Tooltip>
                          </Group>
                        )}
                        <ResultsTable results={results} />
                     </Paper>
                 </Stack>
             </Grid.Col>
          </Grid>
      </Tabs>
      
      {/* Footer */}
      <Box mt="xl" pt="md" style={{ borderTop: '1px solid #eee', textAlign: 'center', color: '#666', fontSize: '12px' }}>
        <Text size="xs" c="dimmed">© 2026 HEALER. All rights reserved. | 
          <Anchor href="https://github.com/eneskelestemur/healer" target="_blank" inherit style={{ color: '#666', textDecoration: 'none' }}> GitHub</Anchor> | 
          <Anchor href="https://www.unc.edu/about/privacy-statement/" target="_blank" inherit style={{ color: '#666', textDecoration: 'none' }}> Privacy</Anchor>
        </Text>
      </Box>
    </Container>
  );
}

export default App;
