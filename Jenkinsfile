pipeline {
    agent any

    environment {
        // Replace with your Google Cloud Ubuntu VM details
        VM_HOST = 'YOUR_GCP_VM_EXTERNAL_IP'      // e.g., 34.123.45.67
        VM_USER = 'ubuntu'                        // default Ubuntu user on GCP
        APP_DIR = '/home/ubuntu/headless-bot'     // application folder on VM
        SSH_CRED_ID = 'gcp-vm-ssh-key'           // Credentials ID configured in Jenkins
    }

    stages {
        stage('Checkout Source') {
            steps {
                echo "📥 Checking out latest code from GitHub..."
                checkout scm
            }
        }

        stage('Deploy to Google Cloud VM') {
            steps {
                echo "🚀 Deploying to Google Cloud VM (${VM_HOST})..."
                sshagent([env.SSH_CRED_ID]) {
                    sh """
                        # 1. Ensure target directory exists on VM
                        ssh -o StrictHostKeyChecking=no ${VM_USER}@${VM_HOST} "mkdir -p ${APP_DIR}"

                        # 2. Transfer updated code files (preserving .env on the VM)
                        scp -o StrictHostKeyChecking=no main.py requirements.txt ${VM_USER}@${VM_HOST}:${APP_DIR}/

                        # 3. Update dependencies and restart bot service on the VM
                        ssh -o StrictHostKeyChecking=no ${VM_USER}@${VM_HOST} << 'EOF'
                            cd ${APP_DIR}

                            # Create venv if not existing
                            if [ ! -d "venv" ]; then
                                python3 -m venv venv
                            fi

                            # Install/update packages
                            source venv/bin/activate
                            pip install --upgrade pip
                            pip install -r requirements.txt
                            playwright install --with-deps chromium

                            # Restart the background systemd service
                            if systemctl is-active --quiet grafana-bot; then
                                echo "🔄 Restarting grafana-bot service..."
                                sudo systemctl restart grafana-bot
                            else
                                echo "ℹ️ grafana-bot service not running yet. Run 'sudo systemctl enable --now grafana-bot' on VM."
                            fi
EOF
                    """
                }
            }
        }
    }

    post {
        success {
            echo "✅ Deployment Successful! Headless Bot updated & active."
        }
        failure {
            echo "❌ Deployment Failed. Check Jenkins logs for details."
        }
    }
}
